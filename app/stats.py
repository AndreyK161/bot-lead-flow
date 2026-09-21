"""Minimal CRM tracking and daily distribution statistics."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app import store
from app.bitrix_client import BitrixClient
from app.config import get_settings
from app.telegram_client import send_telegram_message

logger = logging.getLogger(__name__)


def _setting(name: str, default):
    try:
        return getattr(get_settings(), name, default)
    except Exception:
        # Keeps the pure storage helpers usable in migrations/tests before a full .env exists.
        return default


def _created_date(raw: object, now: datetime | None = None) -> str:
    timezone = ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    if raw:
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone)
            return value.astimezone(timezone).date().isoformat()
        except ValueError:
            logger.warning("Cannot parse Bitrix DATE_CREATE=%r", raw)
    return (now or datetime.now(timezone)).astimezone(timezone).date().isoformat()


def _event(entity_type: str, entity_id: str, event_type: str, old_value, new_value) -> None:
    store.execute(
        "INSERT INTO crm_item_events (entity_type,entity_id,event_type,old_value,new_value) VALUES (?,?,?,?,?)",
        (entity_type, entity_id, event_type, old_value, new_value),
    )


def record(
    entity_type: str,
    item: dict,
    *,
    source_name: str | None = None,
    assignee_name: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Upsert a lead/deal and freeze attribution on its first exit from NEW."""
    entity_id = str(item["ID"])
    stage_field = "STATUS_ID" if entity_type == "lead" else "STAGE_ID"
    unprocessed = (
        _setting("lead_unprocessed_status_id", "NEW")
        if entity_type == "lead"
        else _setting("deal_unprocessed_stage_id", "NEW")
    )
    stage_id = str(item.get(stage_field) or "")
    assignee_id = str(item.get("ASSIGNED_BY_ID") or "")
    source_id = str(item.get("SOURCE_ID") or "")
    existing_rows = store.rows(
        "SELECT * FROM crm_items WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    )
    existing = existing_rows[0] if existing_rows else None
    timestamp = (now or datetime.now(ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow")))).isoformat()

    processed_at = existing["processed_at"] if existing else None
    processed_by_id = existing["processed_by_id"] if existing else None
    processed_by_name = existing["processed_by_name"] if existing else None
    if not processed_at and stage_id and stage_id != unprocessed:
        processed_at = timestamp
        processed_by_id = assignee_id or None
        processed_by_name = assignee_name or None

    if existing:
        resolved_source_name = (
            source_name or existing["source_name"]
            if existing["source_id"] == source_id else source_name
        )
        resolved_assignee_name = (
            assignee_name or existing["current_assignee_name"]
            if existing["current_assignee_id"] == assignee_id else assignee_name
        )
        if existing["current_stage_id"] != stage_id:
            _event(entity_type, entity_id, "stage_changed", existing["current_stage_id"], stage_id)
        if existing["current_assignee_id"] != assignee_id:
            _event(entity_type, entity_id, "assignee_changed", existing["current_assignee_id"], assignee_id)
        store.execute(
            """UPDATE crm_items SET source_id=?,source_name=?,current_stage_id=?,
               current_assignee_id=?,current_assignee_name=?,processed_at=?,processed_by_id=?,
               processed_by_name=?,updated_at=? WHERE entity_type=? AND entity_id=?""",
            (source_id, resolved_source_name, stage_id, assignee_id, resolved_assignee_name,
             processed_at, processed_by_id, processed_by_name, timestamp, entity_type, entity_id),
        )
    else:
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,source_id,source_name,created_date,current_stage_id,
                current_assignee_id,current_assignee_name,processed_at,processed_by_id,processed_by_name,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_type, entity_id, source_id, source_name, _created_date(item.get("DATE_CREATE"), now),
             stage_id, assignee_id, assignee_name, processed_at, processed_by_id, processed_by_name, timestamp),
        )
        _event(entity_type, entity_id, "created", None, stage_id)
    return store.rows(
        "SELECT * FROM crm_items WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)
    )[0]


async def observe(entity_type: str, item: dict, client: BitrixClient | None = None) -> dict | None:
    if entity_type == "deal" and str(item.get("CATEGORY_ID")) != _setting("track_deal_category_id", "0"):
        return None
    client = client or BitrixClient()
    existing = store.rows(
        "SELECT * FROM crm_items WHERE entity_type=? AND entity_id=?", (entity_type, str(item["ID"]))
    )
    source_id = str(item.get("SOURCE_ID") or "")
    assignee_id = str(item.get("ASSIGNED_BY_ID") or "")
    source_name = existing[0]["source_name"] if existing and existing[0]["source_id"] == source_id else None
    assignee_name = (
        existing[0]["current_assignee_name"]
        if existing and existing[0]["current_assignee_id"] == assignee_id else None
    )
    if source_id and not source_name:
        source_name = await client.get_source_name(source_id)
    if assignee_id and not assignee_name:
        assignee_name = await client.get_user_name(assignee_id)
    return record(entity_type, item, source_name=source_name, assignee_name=assignee_name)


def build_daily_report(report_date: date | str) -> str:
    day = report_date.isoformat() if isinstance(report_date, date) else report_date
    rows = store.rows("SELECT * FROM crm_items WHERE created_date=?", (day,))
    leads = sum(row["entity_type"] == "lead" for row in rows)
    deals = sum(row["entity_type"] == "deal" for row in rows)
    processed = [row for row in rows if row["processed_at"]]
    pending = [row for row in rows if not row["processed_at"]]

    def label(row: dict, name_key: str, id_key: str, empty: str) -> str:
        return str(row[name_key] or row[id_key] or empty)

    sources = Counter(label(row, "source_name", "source_id", "Без источника") for row in rows)
    managers = Counter(label(row, "processed_by_name", "processed_by_id", "Без ответственного") for row in processed)
    pending_managers = Counter(label(row, "current_assignee_name", "current_assignee_id", "Без ответственного") for row in pending)

    lines = [
        f"📊 <b>Новые обращения за {escape(day)}</b>",
        f"Всего: <b>{len(rows)}</b> (лиды: {leads}, сделки: {deals})",
        f"Обработано: <b>{len(processed)}</b>",
        f"Осталось на стадии «Не обработан»: <b>{len(pending)}</b>",
    ]
    for title, values in (("Источники", sources), ("Обработали", managers), ("Остаток по менеджерам", pending_managers)):
        lines.append(f"\n<b>{title}</b>")
        lines.extend(f"• {escape(name)} — {count}" for name, count in values.most_common())
        if not values:
            lines.append("• нет")
    return "\n".join(lines)


def _report_target(now: datetime) -> date:
    hour, minute = map(int, _setting("daily_stats_time", "23:55").split(":"))
    return now.date() if now.time() >= time(hour, minute) else now.date() - timedelta(days=1)


async def send_due_report(now: datetime | None = None) -> None:
    timezone = ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    current = now.astimezone(timezone) if now else datetime.now(timezone)
    report_date = _report_target(current).isoformat()
    delivered = {row["chat_id"] for row in store.rows(
        "SELECT chat_id FROM daily_report_deliveries WHERE report_date=?", (report_date,)
    )}
    text = build_daily_report(report_date)
    for chat_id in (str(value) for value in get_settings().director_user_id_set):
        if chat_id in delivered:
            continue
        try:
            message = await send_telegram_message(text, chat_id=chat_id)
            store.execute(
                "INSERT INTO daily_report_deliveries VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (report_date, chat_id, message.get("message_id")),
            )
        except Exception:
            logger.exception("Failed to send daily CRM report date=%s chat_id=%s", report_date, chat_id)


async def daily_report_loop() -> None:
    while True:
        try:
            await send_due_report()
        except Exception:
            logger.exception("Daily CRM report loop failed")
        await asyncio.sleep(60)
