"""Durable manager call queue backed by the Lead Flow database."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app import store
from app.bitrix_client import BitrixClient
from app.config import get_settings

logger = logging.getLogger(__name__)
FINAL_ITEM_STATUSES = {
    "answered", "no_answer", "busy", "unavailable", "voicemail", "skipped", "postponed",
}
ACTIVE_ITEM_STATUSES = {"starting", "calling", "connected", "awaiting_confirmation"}
HISTORY_RESULT = {
    "success": "awaiting_confirmation",
    "missed": "no_answer",
    "busy": "busy",
    "notavailable": "unavailable",
}


class DialerError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _phone(value: object) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if len(digits) < 10:
        return ""
    return "7" + digits[-10:]


def _first_phone(item: dict) -> str:
    values = item.get("PHONE") or item.get("phone") or []
    if isinstance(values, str):
        return _phone(values)
    for value in values:
        raw = value.get("VALUE") if isinstance(value, dict) else value
        normalized = _phone(raw)
        if normalized:
            return normalized
    return ""


def _name(item: dict) -> str:
    full = " ".join(
        str(item.get(key) or "").strip()
        for key in ("NAME", "SECOND_NAME", "LAST_NAME")
        if str(item.get(key) or "").strip()
    )
    return full or str(item.get("TITLE") or f"Клиент #{item.get('ID', '')}").strip()


def _dialer_config(bitrix_user_id: str) -> dict[str, str]:
    settings = get_settings()
    config = settings.megafon_dialer_user_map.get(str(bitrix_user_id), {})
    if not settings.megafon_vats_api_url or not settings.megafon_vats_api_key:
        raise DialerError("CRM API МегаФона ещё не настроен.", 503)
    if not config.get("user") and not config.get("group"):
        raise DialerError("Для менеджера не настроен пользователь или группа МегаФона.", 409)
    return config


async def make_call(bitrix_user_id: str, phone: str) -> str:
    settings = get_settings()
    config = _dialer_config(bitrix_user_id)
    payload: dict[str, object] = {"phone": phone, "show_phone": True}
    if config.get("user"):
        payload["user"] = config["user"]
    else:
        payload["group"] = config["group"]
    if config.get("clid"):
        payload["clid"] = config["clid"]
    headers = {"Content-Type": "application/json"}
    params: dict[str, str] = {}
    if settings.megafon_vats_auth_mode == "header":
        headers[settings.megafon_vats_auth_header] = settings.megafon_vats_api_key
    elif settings.megafon_vats_auth_mode == "query":
        params["auth"] = settings.megafon_vats_api_key
    elif settings.megafon_vats_auth_mode == "body":
        payload["auth"] = settings.megafon_vats_api_key
    else:
        raise DialerError("Некорректный режим авторизации CRM API МегаФона.", 500)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.megafon_vats_api_url.rstrip("/") + "/makecall",
                json=payload, headers=headers, params=params,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise DialerError(f"МегаФон отклонил запуск звонка (HTTP {exc.response.status_code}).", 502) from None
    except (httpx.RequestError, ValueError):
        raise DialerError("CRM API МегаФона не ответил корректно.", 502) from None
    call_id = str(data.get("callid") or "") if isinstance(data, dict) else ""
    if not call_id:
        raise DialerError("МегаФон не вернул идентификатор звонка.", 502)
    return call_id


async def filters() -> dict:
    client = BitrixClient()
    leads = await client.get_lead_statuses()
    deal_zero = await client.get_deal_stages("0")
    deal_two = await client.get_deal_stages("2")

    def stages(values: list[dict]) -> list[dict[str, str]]:
        return [
            {"id": str(value.get("STATUS_ID") or ""), "name": str(value.get("NAME") or "")}
            for value in values if value.get("STATUS_ID") and value.get("NAME")
        ]

    return {"lead": stages(leads), "deal": {"0": stages(deal_zero), "2": stages(deal_two)}}


async def create_session(
    *, telegram_id: int, bitrix_user_id: str, manager_name: str,
    entity_type: str, date_from: date, date_to: date,
    category_id: str | None, stage_ids: list[str], auto_advance: bool,
) -> dict:
    if entity_type not in {"lead", "deal"}:
        raise DialerError("Выберите лиды или сделки.")
    if date_from > date_to or (date_to - date_from).days > 366:
        raise DialerError("Выберите корректный период не больше 367 дней.")
    _dialer_config(bitrix_user_id)
    existing = store.rows(
        "SELECT id FROM dialer_sessions WHERE telegram_id=? AND status IN ('active','paused') LIMIT 1",
        (telegram_id,),
    )
    if existing:
        raise DialerError("Сначала завершите или отмените текущую очередь.", 409)

    zone = ZoneInfo(get_settings().daily_stats_timezone)
    start = datetime.combine(date_from, time.min, zone).isoformat()
    end = datetime.combine(date_to + timedelta(days=1), time.min, zone).isoformat()
    client = BitrixClient()
    if entity_type == "lead":
        raw_items = await client.get_dialer_leads(bitrix_user_id, start, end, stage_ids)
        stage_values = await client.get_lead_statuses()
    else:
        category_id = str(category_id or "0")
        if category_id not in {"0", "2"}:
            raise DialerError("Для обзвона доступны категории сделок 0 и 2.")
        raw_items = await client.get_dialer_deals(bitrix_user_id, category_id, start, end, stage_ids)
        stage_values = await client.get_deal_stages(category_id)
    stage_names = {
        str(value.get("STATUS_ID") or ""): str(value.get("NAME") or "")
        for value in stage_values
    }
    contact_ids = [str(item.get("CONTACT_ID")) for item in raw_items if item.get("CONTACT_ID")]
    contacts = {
        str(item.get("ID")): item
        for item in await client.get_contacts_full_by_ids(contact_ids)
    }
    portal = urlparse(get_settings().bitrix_webhook_url).netloc
    prepared: list[dict] = []
    for item in raw_items:
        phone = _first_phone(item)
        contact = contacts.get(str(item.get("CONTACT_ID") or ""), {})
        if not phone:
            phone = _first_phone(contact)
        if not phone:
            continue
        stage_id = str(item.get("STATUS_ID" if entity_type == "lead" else "STAGE_ID") or "")
        entity_id = str(item.get("ID") or "")
        prepared.append({
            "entity_id": entity_id,
            "client_name": _name(contact) if contact else _name(item),
            "phone": phone,
            "stage_id": stage_id,
            "stage_name": stage_names.get(stage_id, stage_id),
            "source_name": str(item.get("SOURCE_ID") or ""),
            "bitrix_url": f"https://{portal}/crm/{entity_type}/details/{entity_id}/",
        })
    session_id = str(uuid.uuid4())
    with store.db() as conn:
        conn.execute(
            """INSERT INTO dialer_sessions(
               id,telegram_id,bitrix_user_id,manager_name,entity_type,date_from,date_to,
               category_id,stage_ids_json,status,auto_advance)
               VALUES(?,?,?,?,?,?,?,?,?,'active',?)""",
            (
                session_id, telegram_id, bitrix_user_id, manager_name, entity_type,
                date_from.isoformat(), date_to.isoformat(), category_id,
                json.dumps(stage_ids, ensure_ascii=False), int(auto_advance),
            ),
        )
        for position, item in enumerate(prepared, 1):
            conn.execute(
                """INSERT INTO dialer_queue_items(
                   session_id,position,entity_type,entity_id,client_name,phone,stage_id,
                   stage_name,source_name,bitrix_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, position, entity_type, item["entity_id"], item["client_name"],
                    item["phone"], item["stage_id"], item["stage_name"],
                    item["source_name"], item["bitrix_url"],
                ),
            )
        if not prepared:
            conn.execute("UPDATE dialer_sessions SET status='completed' WHERE id=?", (session_id,))
    return session_payload(session_id, telegram_id)


def _owned_session(session_id: str, telegram_id: int | None = None) -> dict:
    sql = "SELECT * FROM dialer_sessions WHERE id=?"
    params: tuple[object, ...] = (session_id,)
    if telegram_id is not None:
        sql += " AND telegram_id=?"
        params += (telegram_id,)
    rows = store.rows(sql, params)
    if not rows:
        raise DialerError("Очередь не найдена.", 404)
    return rows[0]


def _summary(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "completed": sum(item["status"] in FINAL_ITEM_STATUSES for item in items),
        "answered": sum(item["status"] == "answered" for item in items),
        "failed": sum(item["status"] in {"no_answer", "busy", "unavailable", "voicemail", "error"} for item in items),
        "remaining": sum(item["status"] in ({"pending", "error"} | ACTIVE_ITEM_STATUSES) for item in items),
    }


def session_payload(session_id: str, telegram_id: int | None = None) -> dict:
    session = _owned_session(session_id, telegram_id)
    items = store.rows(
        "SELECT * FROM dialer_queue_items WHERE session_id=? ORDER BY position,id", (session_id,),
    )
    current = next((item for item in items if item["status"] in ACTIVE_ITEM_STATUSES), None)
    if current is None:
        current = next((item for item in items if item["status"] in {"pending", "error"}), None)
    return {"session": session, "summary": _summary(items), "current": current, "items": items}


def current_session(telegram_id: int) -> dict | None:
    rows = store.rows(
        """SELECT id FROM dialer_sessions WHERE telegram_id=?
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
           created_at DESC LIMIT 1""",
        (telegram_id,),
    )
    return session_payload(rows[0]["id"], telegram_id) if rows else None


async def start_next(session_id: str, telegram_id: int | None = None) -> dict:
    session = _owned_session(session_id, telegram_id)
    if session["status"] in {"completed", "cancelled"}:
        raise DialerError("Очередь уже завершена.", 409)
    active = store.rows(
        """SELECT * FROM dialer_queue_items WHERE session_id=?
           AND status IN ('starting','calling','connected','awaiting_confirmation')
           ORDER BY position LIMIT 1""",
        (session_id,),
    )
    if active:
        return session_payload(session_id, telegram_id)
    candidates = store.rows(
        """SELECT * FROM dialer_queue_items WHERE session_id=? AND status IN ('pending','error')
           ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,position LIMIT 1""",
        (session_id,),
    )
    if not candidates:
        store.execute(
            "UPDATE dialer_sessions SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,),
        )
        return session_payload(session_id, telegram_id)
    item = candidates[0]
    with store.db() as conn:
        cursor = conn.execute(
            """UPDATE dialer_queue_items SET status='starting',updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status IN ('pending','error')""",
            (item["id"],),
        )
        if cursor.rowcount != 1:
            return session_payload(session_id, telegram_id)
        conn.execute(
            """INSERT INTO dialer_call_attempts(item_id,session_id,status)
               VALUES(?,?,'starting')""",
            (item["id"], session_id),
        )
        conn.execute(
            "UPDATE dialer_sessions SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,),
        )
    try:
        call_id = await make_call(str(session["bitrix_user_id"]), str(item["phone"]))
    except DialerError as exc:
        with store.db() as conn:
            conn.execute(
                """UPDATE dialer_queue_items SET status='error',last_result=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""", (exc.message, item["id"]),
            )
            conn.execute(
                """UPDATE dialer_call_attempts SET status='error',error_text=?,finished_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE item_id=? AND call_id IS NULL""",
                (exc.message, item["id"]),
            )
        raise
    with store.db() as conn:
        conn.execute(
            """UPDATE dialer_queue_items SET status='calling',last_call_id=?,attempts_count=attempts_count+1,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""", (call_id, item["id"]),
        )
        conn.execute(
            """UPDATE dialer_call_attempts SET call_id=?,status='calling',updated_at=CURRENT_TIMESTAMP
               WHERE item_id=? AND call_id IS NULL""", (call_id, item["id"]),
        )
    return session_payload(session_id, telegram_id)


async def _comment(item: dict, manager_name: str, result: str) -> None:
    labels = {
        "no_answer": "не ответил", "busy": "занято", "unavailable": "номер недоступен",
        "voicemail": "автоответчик", "failed": "не дозвонились",
    }
    label = labels.get(result)
    if not label:
        return
    try:
        await BitrixClient().add_crm_timeline_comment(
            str(item["entity_type"]), str(item["entity_id"]),
            f"Результат обзвона Lead Flow: {label}. Менеджер: {manager_name}.",
        )
    except Exception:
        logger.exception("Dialer Bitrix comment failed entity=%s:%s", item["entity_type"], item["entity_id"])


async def resolve(
    session_id: str, item_id: int, result: str, telegram_id: int,
) -> dict:
    session = _owned_session(session_id, telegram_id)
    allowed = {"answered", "failed", "voicemail", "skipped", "postponed", "retry"}
    if result not in allowed:
        raise DialerError("Неизвестный результат звонка.")
    rows = store.rows(
        "SELECT * FROM dialer_queue_items WHERE id=? AND session_id=?", (item_id, session_id),
    )
    if not rows:
        raise DialerError("Клиент не найден в очереди.", 404)
    item = rows[0]
    if result == "retry":
        new_status = "pending"
        stored_result = "retry"
    elif result == "failed":
        new_status = "no_answer"
        stored_result = "failed"
    else:
        new_status = result
        stored_result = result
    with store.db() as conn:
        conn.execute(
            """UPDATE dialer_queue_items SET status=?,last_result=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND session_id=?""", (new_status, stored_result, item_id, session_id),
        )
        if item.get("last_call_id"):
            conn.execute(
                """UPDATE dialer_call_attempts SET status='completed',result=?,finished_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE call_id=?""",
                (stored_result, item["last_call_id"]),
            )
        conn.execute(
            "UPDATE dialer_sessions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ("paused" if result == "answered" else "active", session_id),
        )
    await _comment(item, str(session["manager_name"]), stored_result)
    if result != "answered" and int(session["auto_advance"]):
        return await start_next(session_id, telegram_id)
    if result == "answered":
        pending = store.rows(
            "SELECT 1 FROM dialer_queue_items WHERE session_id=? AND status IN ('pending','error') LIMIT 1",
            (session_id,),
        )
        if not pending:
            store.execute(
                "UPDATE dialer_sessions SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (session_id,),
            )
    return session_payload(session_id, telegram_id)


def cancel(session_id: str, telegram_id: int) -> dict:
    _owned_session(session_id, telegram_id)
    store.execute(
        "UPDATE dialer_sessions SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (session_id,),
    )
    return session_payload(session_id, telegram_id)


async def process_callback(data: dict) -> bool:
    call_id = str(data.get("callid") or data.get("call_id") or data.get("callId") or "").strip()
    if not call_id:
        return False
    attempts = store.rows(
        "SELECT * FROM dialer_call_attempts WHERE call_id=? LIMIT 1", (call_id,),
    )
    if not attempts:
        return False
    attempt = attempts[0]
    command = str(data.get("cmd") or "").strip().lower()
    event_type = str(data.get("type") or "").strip()
    direction = str(data.get("direction") or "").strip().lower()
    history_status = str(data.get("status") or "").strip()
    fingerprint = json.dumps(
        [call_id, command, event_type, direction, history_status, str(data.get("start") or ""), str(data.get("duration") or "")],
        ensure_ascii=False, separators=(",", ":"),
    )
    event_key = hashlib.sha256(fingerprint.encode()).hexdigest()
    with store.db() as conn:
        inserted = conn.execute(
            """INSERT INTO dialer_call_events(event_key,call_id,command,event_type,direction,status)
               VALUES(?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (event_key, call_id, command, event_type, direction, history_status),
        )
        if inserted.rowcount != 1:
            return True
        if command == "event" and event_type.upper() == "ACCEPTED":
            conn.execute(
                """UPDATE dialer_call_attempts SET status='connected',connected_at=COALESCE(connected_at,CURRENT_TIMESTAMP),
                   updated_at=CURRENT_TIMESTAMP WHERE call_id=?""", (call_id,),
            )
            conn.execute(
                """UPDATE dialer_queue_items SET status='connected',updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status IN ('starting','calling')""", (attempt["item_id"],),
            )
    if command != "history":
        return True
    result = HISTORY_RESULT.get(history_status.casefold(), "no_answer")
    try:
        duration = max(0, int(float(data.get("duration") or 0)))
    except (TypeError, ValueError):
        duration = 0
    with store.db() as conn:
        conn.execute(
            """UPDATE dialer_call_attempts SET status='completed',result=?,duration_seconds=?,
               finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE call_id=?""",
            (history_status or result, duration, call_id),
        )
        conn.execute(
            """UPDATE dialer_queue_items SET status=?,last_result=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""", (result, history_status or result, attempt["item_id"]),
        )
        conn.execute(
            "UPDATE dialer_sessions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ("paused" if result == "awaiting_confirmation" else "active", attempt["session_id"]),
        )
    if result != "awaiting_confirmation":
        item = store.rows("SELECT * FROM dialer_queue_items WHERE id=?", (attempt["item_id"],))[0]
        session = _owned_session(str(attempt["session_id"]))
        await _comment(item, str(session["manager_name"]), result)
        if int(session["auto_advance"]):
            try:
                await start_next(str(attempt["session_id"]))
            except DialerError:
                logger.exception("Dialer automatic next call failed session=%s", attempt["session_id"])
    return True
