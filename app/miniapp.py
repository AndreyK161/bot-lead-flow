"""Telegram Mini App UI, authentication and live report API."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from app.client_registry import build_clients_workbook, collect_clients
from app.config import get_settings
from app.period_reports import collect_live_period
from app.call_tracking import today_payload
from app.worktime import build_worktime_workbook, collect_worktime
from app.task_reports import build_task_workbook, collect_task_report
from app.standard_exports import build_standard_sales_workbook, collect_standard_sales_export
from app.telephony_exports import build_telephony_workbook, collect_telephony_export
from app import planner_access
from app import store
from app.manager_planner.integrations import PlannerCRM

router = APIRouter()
logger = logging.getLogger(__name__)
HTML_PATH = Path(__file__).with_name("static") / "miniapp.html"
CACHE_SECONDS = 300
REPORT_TIMEOUT_SECONDS = 900
MAX_AUTH_AGE_SECONDS = 86_400
_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_cache_locks: dict[tuple[date, date], asyncio.Lock] = {}
_jobs: dict[str, dict] = {}
JOB_TTL_SECONDS = 900
PRESENCE_CACHE_SECONDS = 30
_presence_cache: tuple[float, dict] | None = None
_clients_cache: tuple[float, dict] | None = None
_clients_lock = asyncio.Lock()
_worktime_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_task_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_sales_export_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_telephony_export_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_manager_stats_cache: dict[tuple[int, date], tuple[float, dict]] = {}


def validate_init_data(init_data: str, bot_token: str, *, now: int | None = None) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("missing hash")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("invalid hash")
    auth_date = int(values.get("auth_date") or 0)
    current = int(time.time()) if now is None else now
    if not auth_date or current - auth_date > MAX_AUTH_AGE_SECONDS or auth_date > current + 60:
        raise ValueError("expired auth data")
    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("missing user") from exc


def _authorized_user(request: Request) -> int:
    settings = get_settings()
    try:
        user = validate_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token,
        )
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Open this report from Telegram")
    allowed_users = (
        settings.director_user_id_set
        | settings.admin_user_id_set
        | getattr(settings, "super_admin_user_id_set", set())
    )
    if user_id not in allowed_users and user_id not in planner_access.enabled_user_ids(role="supervisor"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return user_id


def _source_rows(
    items: list[dict], stage_key: str, entity_key: str, entity_type: str, portal_domain: str,
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["source_name"], []).append(item)
    result = []
    for source, source_items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        stage_groups: dict[str, list[dict]] = {}
        for item in source_items:
            stage_groups.setdefault(item[stage_key], []).append(item)
        result.append({
            "source": source,
            "total": len(source_items),
            "stages": [
                {
                    "name": name,
                    "count": len(stage_items),
                    "items": [
                        {
                            "id": item[entity_key],
                            "url": f"https://{portal_domain}/crm/{entity_type}/details/{item[entity_key]}/",
                            "manager_id": item["manager_id"],
                            "manager_name": item["manager_name"],
                            "converted": bool(item.get("converted")),
                            "direct": not bool(item.get("converted_from_report_lead")),
                            "contract": bool(item.get("contract")),
                            "deal_id": str(item.get("deal_id") or "") if entity_type == "lead" else "",
                            "deal_url": (
                                f"https://{portal_domain}/crm/deal/details/{item['deal_id']}/"
                                if entity_type == "lead" and item.get("deal_id") else ""
                            ),
                        }
                        for item in stage_items
                    ],
                }
                for name, stage_items in sorted(
                    stage_groups.items(), key=lambda pair: (-len(pair[1]), pair[0]),
                )
            ],
            "contracts": sum(bool(item.get("contract")) for item in source_items),
        })
    return result


def report_payload(data: dict) -> dict:
    leads = data["leads"]
    deals = data["deals"]
    return {
        "period": {
            "start": data["start"].isoformat(),
            "end": data["end"].isoformat(),
            "checked_at": data["checked_at"].isoformat(),
        },
        "totals": {
            "unique": data["unique_total"],
            "leads": len(leads),
            "direct_deals": data["direct_deal_count"],
            "converted": sum(bool(item["converted"]) for item in leads),
            "deals": len(deals),
            "contracts": sum(bool(item["contract"]) for item in deals),
        },
        "managers": data["managers"],
        "leads": _source_rows(
            leads, "result_stage", "lead_id", "lead", data["portal_domain"],
        ),
        "deals": _source_rows(
            deals, "stage", "deal_id", "deal", data["portal_domain"],
        ),
    }


async def _cached_report(start: date, end: date, progress=None) -> dict:
    key = (start, end)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        if progress:
            progress(100, "Готово")
        return cached[1]
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    if lock.locked() and progress:
        progress(3, "Ожидаю уже запущенную сверку этого периода")
    async with lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            if progress:
                progress(100, "Готово")
            return cached[1]
        try:
            async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                payload = report_payload(
                    await collect_live_period(start, end, progress=progress),
                )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Слишком большой период: Bitrix не успел подготовить данные. Выберите меньший диапазон.",
            )
        _cache[key] = (time.monotonic(), payload)
        if progress:
            progress(100, "Готово")
        return payload


def _validate_period(start: date, end: date) -> None:
    if start > end:
        raise HTTPException(status_code=400, detail="Start date must not be after end date")
    if (end - start).days > 366:
        raise HTTPException(status_code=400, detail="Maximum period is 367 days")


def _cleanup_jobs() -> None:
    cutoff = time.monotonic() - JOB_TTL_SECONDS
    for job_id, job in list(_jobs.items()):
        if job["created_at"] < cutoff and job["status"] != "running":
            _jobs.pop(job_id, None)


async def _run_report_job(job_id: str, start: date, end: date) -> None:
    job = _jobs[job_id]

    def update(percent: int, message: str) -> None:
        job["progress"] = max(job["progress"], min(100, percent))
        job["message"] = message

    try:
        job["result"] = await _cached_report(start, end, update)
        job["status"] = "done"
        update(100, "Готово")
    except Exception as exc:
        logger.exception("Mini App report job failed job_id=%s", job_id)
        job["status"] = "error"
        job["message"] = exc.detail if isinstance(exc, HTTPException) else "Не удалось получить данные из Bitrix"


@router.get("/miniapp", response_class=HTMLResponse)
async def mini_app_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@router.get("/api/miniapp/report")
async def mini_app_report(request: Request, start: date, end: date) -> dict:
    _authorized_user(request)
    _validate_period(start, end)
    return await _cached_report(start, end)


@router.get("/api/miniapp/access")
async def mini_app_access(request: Request) -> dict:
    _authorized_user(request)
    return {"calls": True}


@router.get("/api/miniapp/calls/today")
async def mini_app_calls_today(request: Request) -> dict:
    _authorized_user(request)
    return await today_payload()


async def _cached_clients() -> dict:
    global _clients_cache
    now = time.monotonic()
    if _clients_cache and now - _clients_cache[0] < CACHE_SECONDS:
        return _clients_cache[1]
    async with _clients_lock:
        now = time.monotonic()
        if _clients_cache and now - _clients_cache[0] < CACHE_SECONDS:
            return _clients_cache[1]
        payload = await collect_clients()
        _clients_cache = (now, payload)
        return payload


@router.get("/api/miniapp/clients")
async def mini_app_clients(request: Request) -> dict:
    _authorized_user(request)
    return await _cached_clients()


@router.get("/api/miniapp/clients.xlsx")
async def mini_app_clients_excel(request: Request) -> Response:
    _authorized_user(request)
    content = build_clients_workbook(await _cached_clients())
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="clients.xlsx"'},
    )


async def _cached_worktime(start: date, end: date) -> dict:
    key = (start, end)
    cached = _worktime_cache.get(key)
    if cached and time.monotonic() - cached[0] < PRESENCE_CACHE_SECONDS:
        return cached[1]
    payload = await collect_worktime(start, end)
    _worktime_cache[key] = (time.monotonic(), payload)
    return payload


def _validate_worktime_period(start: date, end: date) -> None:
    if start > end or (end - start).days > 31:
        raise HTTPException(status_code=400, detail="Выберите период не больше 32 дней")


@router.get("/api/miniapp/worktime")
async def mini_app_worktime(request: Request, start: date, end: date) -> dict:
    _authorized_user(request)
    _validate_worktime_period(start, end)
    return await _cached_worktime(start, end)


@router.get("/api/miniapp/worktime.xlsx")
async def mini_app_worktime_excel(request: Request, start: date, end: date) -> Response:
    _authorized_user(request)
    _validate_worktime_period(start, end)
    content = build_worktime_workbook(await _cached_worktime(start, end))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="worktime.xlsx"'},
    )


async def _cached_tasks(start: date, end: date) -> dict:
    key = (start, end)
    cached = _task_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    payload = await collect_task_report(start, end)
    _task_cache[key] = (time.monotonic(), payload)
    return payload


@router.get("/api/miniapp/tasks")
async def mini_app_tasks(request: Request, start: date, end: date) -> dict:
    _authorized_user(request)
    _validate_worktime_period(start, end)
    return await _cached_tasks(start, end)


@router.get("/api/miniapp/tasks.xlsx")
async def mini_app_tasks_excel(request: Request, start: date, end: date) -> Response:
    _authorized_user(request)
    _validate_worktime_period(start, end)
    content = build_task_workbook(await _cached_tasks(start, end))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tasks.xlsx"'},
    )


@router.get("/api/miniapp/sales.xlsx")
async def mini_app_standard_sales_excel(request: Request, start: date, end: date) -> Response:
    _authorized_user(request)
    _validate_period(start, end)
    key = (start, end)
    cached = _sales_export_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        payload = cached[1]
    else:
        payload = await collect_standard_sales_export(start, end)
        _sales_export_cache[key] = (time.monotonic(), payload)
    return Response(
        content=build_standard_sales_workbook(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sales.xlsx"'},
    )


@router.get("/api/miniapp/telephony.xlsx")
async def mini_app_telephony_excel(request: Request, start: date, end: date) -> Response:
    _authorized_user(request)
    _validate_period(start, end)
    key = (start, end)
    cached = _telephony_export_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        payload = cached[1]
    else:
        payload = await collect_telephony_export(start, end)
        _telephony_export_cache[key] = (time.monotonic(), payload)
    return Response(
        content=build_telephony_workbook(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="telephony.xlsx"'},
    )


def _manager_name_key(value: object) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(value or "").casefold().replace("ё", "е"))


def _manager_bitrix_map() -> dict[str, int]:
    """Join presence names to stable Bitrix IDs without exposing manager logins."""
    result: dict[str, int] = {}
    identities = store.list_planner_access()
    for item in identities:
        bitrix_id = str(item.get("bitrix_user_id") or "")
        if item.get("role") == "manager" and item.get("active") and bitrix_id.isdigit():
            result[_manager_name_key(item.get("display_name"))] = int(bitrix_id)
    for item in store.list_links():
        bitrix_id = str(item.get("bitrix_user_id") or "")
        if bitrix_id.isdigit():
            result.setdefault(_manager_name_key(item.get("bitrix_name")), int(bitrix_id))
    return {key: value for key, value in result.items() if key}


def _presence_payload(items: list[dict]) -> dict:
    allowed_statuses = {"working", "break", "offline", "unknown"}
    bitrix_ids = _manager_bitrix_map()
    managers = []
    for item in items:
        presence_status = str(item.get("presence_status") or "unknown")
        if presence_status not in allowed_statuses:
            presence_status = "unknown"
        name = str(item.get("full_name") or "Менеджер")
        managers.append({
            "id": item.get("id"),
            "name": name,
            "bitrix_id": bitrix_ids.get(_manager_name_key(name)),
            "status": presence_status,
            "changed_at": item.get("presence_changed_at"),
            "checked_at": item.get("presence_checked_at"),
            "workday_started_at": item.get("workday_started_at"),
            "lunch_started_at": item.get("lunch_started_at"),
        })
    checked = [item["checked_at"] for item in managers if item.get("checked_at")]
    return {
        "managers": managers,
        "checked_at": max(checked) if checked else None,
        "refresh_seconds": 60,
        "unavailable": False,
    }


async def _manager_presence_payload() -> dict:
    global _presence_cache
    now = time.monotonic()
    if _presence_cache and now - _presence_cache[0] < PRESENCE_CACHE_SECONDS:
        return _presence_cache[1]
    settings = get_settings()
    if not settings.leadflow_presence_url or not settings.leadflow_presence_token:
        return {"managers": [], "checked_at": None, "refresh_seconds": 60, "unavailable": True}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                settings.leadflow_presence_url,
                headers={"X-Internal-Token": settings.leadflow_presence_token},
            )
            response.raise_for_status()
        payload = _presence_payload(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        logger.exception("Cannot load manager presence from LeadFlow")
        payload = {"managers": [], "checked_at": None, "refresh_seconds": 60, "unavailable": True}
    _presence_cache = (now, payload)
    return payload


@router.get("/api/miniapp/managers/presence")
async def mini_app_manager_presence(request: Request) -> dict:
    _authorized_user(request)
    return await _manager_presence_payload()


def _allowed_manager_ids() -> set[int]:
    return set(_manager_bitrix_map().values())


@router.get("/api/miniapp/managers/{bitrix_user_id}/stats")
async def mini_app_manager_stats(
    request: Request, bitrix_user_id: int, day: date | None = None,
) -> dict:
    _authorized_user(request)
    if bitrix_user_id not in _allowed_manager_ids():
        raise HTTPException(status_code=404, detail="Менеджер не найден")
    selected_day = day or datetime.now(ZoneInfo("Europe/Moscow")).date()
    key = (bitrix_user_id, selected_day)
    cached = _manager_stats_cache.get(key)
    if cached and time.monotonic() - cached[0] < PRESENCE_CACHE_SECONDS:
        return cached[1]

    presence = await _manager_presence_payload()
    manager = next(
        (item for item in presence.get("managers", []) if item.get("bitrix_id") == bitrix_user_id),
        None,
    )
    if manager is None:
        name = next(
            (
                item.get("display_name") for item in store.list_planner_access()
                if str(item.get("bitrix_user_id") or "") == str(bitrix_user_id)
            ),
            f"Менеджер #{bitrix_user_id}",
        )
        manager = {"name": name, "status": "unknown"}

    crm = PlannerCRM()
    calls, leads = await asyncio.gather(
        crm.call_statistics(bitrix_user_id, selected_day.isoformat()),
        crm.processed_leads(bitrix_user_id, selected_day.isoformat()),
    )
    result = {
        "day": selected_day.isoformat(),
        "manager": {
            "bitrix_id": bitrix_user_id,
            "name": manager["name"],
            "status": manager.get("status", "unknown"),
        },
        "leads": leads,
        "calls": calls,
    }
    _manager_stats_cache[key] = (time.monotonic(), result)
    return result


@router.post("/api/miniapp/report/jobs")
async def start_mini_app_report_job(request: Request, start: date, end: date) -> dict:
    user_id = _authorized_user(request)
    _validate_period(start, end)
    _cleanup_jobs()
    for job_id, job in _jobs.items():
        if job["user_id"] == user_id and job["start"] == start and job["end"] == end and job["status"] == "running":
            return {"job_id": job_id}
    job_id = uuid4().hex
    job = {
        "user_id": user_id,
        "start": start,
        "end": end,
        "status": "running",
        "progress": 1,
        "message": "Готовлю запрос",
        "result": None,
        "created_at": time.monotonic(),
    }
    _jobs[job_id] = job
    job["task"] = asyncio.create_task(_run_report_job(job_id, start, end))
    return {"job_id": job_id}


@router.get("/api/miniapp/report/jobs/{job_id}")
async def mini_app_report_job(request: Request, job_id: str) -> dict:
    user_id = _authorized_user(request)
    job = _jobs.get(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Report job not found")
    response = {
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
    }
    if job["status"] == "done":
        response["result"] = job["result"]
    return response
