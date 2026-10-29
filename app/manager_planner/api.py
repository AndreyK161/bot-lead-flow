"""Private manager-mode Planner API mounted inside Lead Flow."""
from __future__ import annotations

import logging
import hashlib
import sqlite3
import time
from datetime import date
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app.config import get_settings
from app import dialer, planner_access as access_registry

from .auth import InvalidInitData, InvalidTelegramLogin, issue_web_session, validate_init_data, validate_login_widget, validate_web_session
from .integrations import CRM_STATUS, PlannerCRM
from .models import DecisionInput, DialerResultInput, DialerSessionInput, EmployeeInput, EmployeePatch, GoalInput, MoveInput, PlanInput, TaskInput, TaskPatch, TimerInput
from .store import DomainError, Store, today

WORKDAY = {"start": "10:00", "end": "19:00", "timezone": "Europe/Moscow", "reminders_enabled": False}
logger = logging.getLogger(__name__)
CRM_CACHE_SECONDS = 60
_crm_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_performance_cache: dict[tuple[int, str, str], tuple[float, dict]] = {}
_web_identity_cache: dict[str, tuple[float, dict]] = {}
WEB_IDENTITY_CACHE_SECONDS = 15


static_path = Path(__file__).resolve().parent.parent / "static" / "manager"
# Configuration is deliberately not loaded at import time: the main test suite
# patches it after importing app.main. No database is opened before initialize().
store = Store(Path("data/manager_planner.sqlite3"))


def initialize() -> None:
    """Create/migrate only the isolated Lead Flow planner database."""
    global store
    configured_path = Path(get_settings().manager_planner_database_path)
    if store.path != configured_path:
        store = Store(configured_path)
    store.initialize()


def create_app() -> FastAPI:
    app = FastAPI(title="План дня · Lead Flow", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"detail": "Запись уже существует или конфликтует с другим изменением. Обновите данные."}, status_code=409)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse({"detail": "Хранилище временно недоступно. Повторите позже."}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": "Проверьте заполнение полей и допустимые значения."}, status_code=422)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-src https://oauth.telegram.org https://telegram.org; "
            "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
        )
        return response

    def telegram_actor(telegram_id: int) -> dict:
        identity = access_registry.authorized_identity(telegram_id)
        if identity is None:
            raise HTTPException(403, "Доступ к Planner пока не открыт.")
        user = store.actor(telegram_id)
        if identity["role"] != "owner" and user["role"] != identity["role"]:
            raise HTTPException(403, "Роль Planner не совпадает с перенесённой ролью.")
        if identity["role"] == "manager" and str(user.get("bitrix_id") or "") != str(identity["bitrix_user_id"] or ""):
            raise HTTPException(403, "Привязка менеджера к Bitrix не совпадает.")
        return user

    @app.get("/api/auth/telegram/config")
    async def telegram_auth_config():
        username = get_settings().telegram_bot_username.strip().lstrip("@")
        if not username:
            raise HTTPException(503, "Вход через Telegram пока не настроен.")
        return {"bot_username": username}

    @app.post("/api/auth/telegram")
    async def telegram_auth(payload: dict = Body(...)):
        settings = get_settings()
        try:
            telegram_id = validate_login_widget(
                payload, settings.telegram_bot_token,
                max_age=settings.telegram_login_max_age_seconds,
            )
        except InvalidTelegramLogin as exc:
            raise HTTPException(401, str(exc)) from None
        telegram_actor(telegram_id)
        return {
            "access_token": issue_web_session(
                telegram_id, settings.telegram_bot_token,
                ttl=settings.telegram_web_session_seconds,
            ),
            "token_type": "telegram",
            "expires_in": settings.telegram_web_session_seconds,
        }

    async def actor(authorization: str | None = Header(default=None)):
        settings = get_settings()
        if authorization and authorization.startswith("tma "):
            try:
                telegram_id = validate_init_data(authorization[4:], settings.telegram_bot_token)
            except InvalidInitData as exc:
                raise HTTPException(401, str(exc)) from None
            return telegram_actor(telegram_id)
        if authorization and authorization.startswith("tgweb "):
            try:
                telegram_id = validate_web_session(authorization[6:].strip(), settings.telegram_bot_token)
            except InvalidTelegramLogin as exc:
                raise HTTPException(401, str(exc)) from None
            return telegram_actor(telegram_id)
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Войдите через Telegram, Lead Flow или откройте приложение из бота.")
        if not settings.leadflow_web_auth_url:
            raise HTTPException(503, "Веб-вход Planner пока не включён.")
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(401, "Токен входа отсутствует.")
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        cached = _web_identity_cache.get(cache_key)
        web_user = cached[1] if cached and time.monotonic() - cached[0] < WEB_IDENTITY_CACHE_SECONDS else None
        if web_user is None:
            try:
                async with httpx.AsyncClient(timeout=settings.leadflow_web_auth_timeout_seconds) as client:
                    response = await client.get(
                        settings.leadflow_web_auth_url,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.RequestError:
                raise HTTPException(503, "Сервис авторизации Lead Flow временно недоступен.") from None
            if response.status_code == 401:
                raise HTTPException(401, "Сессия истекла. Войдите снова.")
            if response.status_code != 200:
                raise HTTPException(403, "Доступ к Planner не подтверждён.")
            try:
                web_user = response.json()
            except ValueError:
                raise HTTPException(502, "Сервис авторизации вернул некорректный ответ.") from None
            if not isinstance(web_user, dict) or not web_user.get("is_active"):
                raise HTTPException(403, "Учётная запись отключена.")
            _web_identity_cache[cache_key] = (time.monotonic(), web_user)
        role = str(web_user.get("role") or "")
        with store.connection() as connection:
            if role == "manager":
                bitrix_id = str(web_user.get("bitrix_user_id") or "")
                row = connection.execute(
                    "SELECT * FROM employees WHERE role='manager' AND active=1 AND bitrix_id=?",
                    (bitrix_id,),
                ).fetchone() if bitrix_id else None
            elif role in {"admin", "supervisor"}:
                row = connection.execute(
                    "SELECT * FROM employees WHERE role='supervisor' AND active=1 ORDER BY id LIMIT 1"
                ).fetchone()
            else:
                row = None
        if row is None:
            raise HTTPException(403, "Учётная запись Lead Flow не привязана к сотруднику Planner.")
        user = dict(row)
        user["name"] = str(web_user.get("full_name") or user["name"])
        user["web_user_id"] = int(web_user.get("id") or 0)
        user["auth_transport"] = "web"
        return user

    async def sync_plan_tasks(user: dict, plan: dict) -> None:
        if plan["status"] == "closed":
            return
        employee = store.employee(user, plan["employee_id"])
        if not employee.get("bitrix_id"):
            return
        try:
            external = await PlannerCRM().tasks(int(employee["bitrix_id"]), plan["day"])
            store.sync_external_tasks(plan["id"], external)
        except Exception:
            # Bitrix availability never blocks the employee's manual plan.
            logger.exception(
                "Planner Bitrix task sync failed employee_id=%s day=%s",
                employee["id"], plan["day"],
            )

    @app.get("/api/me")
    def me(user=Depends(actor)):
        return {"user": user, "today": today(), "timezone": "Europe/Moscow", "workday": WORKDAY, "crm": CRM_STATUS}

    @app.get("/healthz", include_in_schema=False)
    def health():
        try:
            with store.connection() as connection:
                connection.execute("SELECT 1 FROM plans LIMIT 1").fetchone()
        except sqlite3.Error:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/api/employees")
    def employees(user=Depends(actor)):
        return store.employees(user)

    @app.get("/api/distributions")
    def distributions(employee_id: int | None = None, user=Depends(actor)):
        from .sync import distribution_notices
        return distribution_notices(store, user, employee_id)

    @app.post("/api/employees", status_code=201)
    def add_employee(body: EmployeeInput, user=Depends(actor)):
        return store.save_employee(user, body.model_dump())

    @app.patch("/api/employees/{employee_id}")
    def edit_employee(employee_id: int, body: EmployeePatch, user=Depends(actor)):
        return store.save_employee(user, body.model_dump(), employee_id)

    @app.get("/api/plans")
    def plans(employee_id: int | None = None, day: date | None = None, user=Depends(actor)):
        return store.list_plans(user, employee_id, day.isoformat() if day else None)

    @app.post("/api/plans", status_code=201)
    async def add_plan(body: PlanInput, user=Depends(actor)):
        result = store.create_plan(user, body.day.isoformat(), body.employee_id, body.from_previous)
        await sync_plan_tasks(user, result)
        return store.get_plan(user, result["id"])

    @app.get("/api/plans/{plan_id}")
    async def plan(plan_id: int, user=Depends(actor)):
        result = store.get_plan(user, plan_id)
        await sync_plan_tasks(user, result)
        return store.get_plan(user, plan_id)

    @app.get("/api/crm/summary")
    async def crm_summary(employee_id: int | None = None, day: date | None = None, user=Depends(actor)):
        employee = store.employee(user, employee_id)
        selected_day = (day or date.fromisoformat(today())).isoformat()
        if not employee.get("bitrix_id"):
            return {
                "available": False, "day": selected_day,
                "message": "Сотрудник не привязан к Bitrix24.",
            }
        key = (int(employee["bitrix_id"]), selected_day)
        cached = _crm_cache.get(key)
        if cached and time.monotonic() - cached[0] < CRM_CACHE_SECONDS:
            return cached[1]
        try:
            result = await PlannerCRM().summary(key[0], key[1])
        except Exception:
            logger.exception("Planner CRM summary failed bitrix_id=%s day=%s", *key)
            return {
                "available": False, "day": selected_day,
                "message": "Bitrix24 временно недоступен. План продолжает работать.",
            }
        _crm_cache[key] = (time.monotonic(), result)
        return result

    @app.get("/api/crm/performance")
    async def crm_performance(
        employee_id: int | None = None,
        start: date | None = None,
        end: date | None = None,
        user=Depends(actor),
    ):
        # Store.employee enforces ownership: managers can only resolve themselves.
        employee = store.employee(user, employee_id)
        last_day = end or date.fromisoformat(today())
        first_day = start or last_day
        if first_day > last_day or (last_day - first_day).days > 366:
            raise HTTPException(400, "Выберите корректный период не больше 367 дней.")
        period = {"start": first_day.isoformat(), "end": last_day.isoformat()}
        if not employee.get("bitrix_id"):
            return {
                "available": False, "period": period,
                "message": "Сотрудник не привязан к Bitrix24.",
            }
        key = (int(employee["bitrix_id"]), period["start"], period["end"])
        cached = _performance_cache.get(key)
        if cached and time.monotonic() - cached[0] < CRM_CACHE_SECONDS:
            return cached[1]
        try:
            result = await PlannerCRM().performance(*key)
        except Exception:
            logger.exception(
                "Planner CRM performance failed bitrix_id=%s start=%s end=%s", *key,
            )
            return {
                "available": False, "period": period,
                "message": "Bitrix24 временно недоступен. Повторите загрузку позже.",
            }
        result["employee"] = {"id": employee["id"], "name": employee["name"]}
        _performance_cache[key] = (time.monotonic(), result)
        return result

    @app.get("/api/dialer/filters")
    async def dialer_filters(user=Depends(actor)):
        try:
            return await dialer.filters()
        except Exception:
            logger.exception("Dialer filters failed")
            raise HTTPException(502, "Не удалось получить стадии из Bitrix24.") from None

    @app.get("/api/dialer/session")
    def dialer_session(user=Depends(actor)):
        return dialer.current_session(int(user["telegram_id"]))

    @app.post("/api/dialer/sessions", status_code=201)
    async def create_dialer_session(body: DialerSessionInput, user=Depends(actor)):
        employee = store.employee(user, body.employee_id)
        if not employee.get("bitrix_id"):
            raise HTTPException(409, "Менеджер не привязан к пользователю Bitrix24.")
        try:
            return await dialer.create_session(
                telegram_id=int(user["telegram_id"]),
                bitrix_user_id=str(employee["bitrix_id"]),
                manager_name=str(employee["name"]),
                entity_type=body.entity_type,
                date_from=body.date_from,
                date_to=body.date_to,
                category_id=body.category_id,
                stage_ids=body.stage_ids,
                auto_advance=body.auto_advance,
            )
        except dialer.DialerError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.post("/api/dialer/sessions/{session_id}/start")
    async def start_dialer_session(session_id: str, user=Depends(actor)):
        try:
            return await dialer.start_next(session_id, int(user["telegram_id"]))
        except dialer.DialerError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.post("/api/dialer/sessions/{session_id}/result")
    async def dialer_result(session_id: str, body: DialerResultInput, user=Depends(actor)):
        try:
            return await dialer.resolve(
                session_id, body.item_id, body.result, int(user["telegram_id"]),
            )
        except dialer.DialerError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.post("/api/dialer/sessions/{session_id}/cancel")
    def cancel_dialer_session(session_id: str, user=Depends(actor)):
        try:
            return dialer.cancel(session_id, int(user["telegram_id"]))
        except dialer.DialerError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.post("/api/plans/{plan_id}/approve")
    def approve(plan_id: int, user=Depends(actor)):
        return store.approve_plan(user, plan_id)

    @app.post("/api/plans/{plan_id}/confirm")
    def confirm(plan_id: int, user=Depends(actor)):
        return store.confirm_plan(user, plan_id)

    @app.post("/api/plans/{plan_id}/close")
    def close(plan_id: int, user=Depends(actor)):
        return store.close_plan(user, plan_id)

    @app.get("/api/plans/{plan_id}/report")
    def report(plan_id: int, user=Depends(actor)):
        return store.report(user, plan_id)

    @app.get("/api/plans/{plan_id}/history")
    def history(plan_id: int, user=Depends(actor)):
        return store.history(user, plan_id)

    @app.post("/api/plans/{plan_id}/tasks", status_code=201)
    def add_task(plan_id: int, body: TaskInput, user=Depends(actor)):
        return store.add_task(user, plan_id, body.model_dump())

    @app.patch("/api/tasks/{task_id}")
    def edit_task(task_id: int, body: TaskPatch, user=Depends(actor)):
        return store.patch_task(user, task_id, body.model_dump(exclude_unset=True))

    @app.post("/api/tasks/{task_id}/timer")
    def timer(task_id: int, body: TimerInput, user=Depends(actor)):
        return store.timer(user, task_id, body.action)

    @app.post("/api/tasks/{task_id}/move")
    def move(task_id: int, body: MoveInput, user=Depends(actor)):
        return store.move(user, task_id, body.day.isoformat(), body.reason, body.revision)

    @app.post("/api/tasks/{task_id}/requests", status_code=201)
    def goal_request(task_id: int, body: GoalInput, user=Depends(actor)):
        return store.request_goal(user, task_id, body.model_dump())

    @app.get("/api/requests")
    def requests(user=Depends(actor)):
        return store.requests(user)

    @app.post("/api/requests/{request_id}/decision")
    def decision(request_id: int, body: DecisionInput, user=Depends(actor)):
        return store.decide(user, request_id, body.decision, body.comment)

    if static_path.is_dir():
        app.mount("/", StaticFiles(directory=static_path, html=True), name="manager-miniapp")
    return app


app = create_app()
