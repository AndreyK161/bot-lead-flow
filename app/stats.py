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


def _bitrix_datetime(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    timezone = ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _event(
    entity_type: str,
    entity_id: str,
    event_type: str,
    old_value,
    new_value,
    occurred_at: str,
) -> None:
    store.execute(
        "INSERT INTO crm_item_events "
        "(entity_type,entity_id,event_type,old_value,new_value,occurred_at) VALUES (?,?,?,?,?,?)",
        (entity_type, entity_id, event_type, old_value, new_value, occurred_at),
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
    event_time = now or _bitrix_datetime(item.get("DATE_MODIFY")) or datetime.now(
        ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    )
    timestamp = event_time.isoformat()
    if existing:
        existing_updated_at = _as_datetime(existing["updated_at"])
        if existing_updated_at and event_time < existing_updated_at:
            logger.info(
                "Ignoring stale %s update entity_id=%s event_time=%s current_time=%s",
                entity_type, entity_id, timestamp, existing["updated_at"],
            )
            return existing

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
            _event(
                entity_type, entity_id, "stage_changed",
                existing["current_stage_id"], stage_id, timestamp,
            )
        if existing["current_assignee_id"] != assignee_id:
            event_type = "transferred" if existing["processed_at"] else "assignee_changed"
            _event(
                entity_type, entity_id, event_type,
                existing["current_assignee_id"], assignee_id, timestamp,
            )
        store.execute(
            """UPDATE crm_items SET source_id=?,source_name=?,current_stage_id=?,
               current_assignee_id=?,current_assignee_name=?,processed_at=?,processed_by_id=?,
               processed_by_name=?,updated_at=?
               WHERE entity_type=? AND entity_id=?""",
            (source_id, resolved_source_name, stage_id, assignee_id, resolved_assignee_name,
             processed_at, processed_by_id, processed_by_name, timestamp, entity_type, entity_id),
        )
    else:
        first_seen_at = (_bitrix_datetime(item.get("DATE_CREATE")) or event_time).isoformat()
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,source_id,source_name,created_date,current_stage_id,
                current_assignee_id,current_assignee_name,processed_at,processed_by_id,
                processed_by_name,first_seen_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_type, entity_id, source_id, source_name, _created_date(item.get("DATE_CREATE"), now),
             stage_id, assignee_id, assignee_name, processed_at, processed_by_id,
             processed_by_name, first_seen_at, timestamp),
        )
        _event(entity_type, entity_id, "created", None, stage_id, timestamp)
    return store.rows(
        "SELECT * FROM crm_items WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)
    )[0]


async def observe(
    entity_type: str,
    item: dict,
    client: BitrixClient | None = None,
    *,
    now: datetime | None = None,
) -> dict | None:
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
    return record(entity_type, item, source_name=source_name, assignee_name=assignee_name, now=now)


def report_bounds(report_date: date | str) -> tuple[datetime, datetime]:
    """Fixed interval ending at the configured report time on report_date."""
    day = report_date if isinstance(report_date, date) else date.fromisoformat(report_date)
    timezone = ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    hour, minute = map(int, _setting("daily_stats_time", "19:00").split(":"))
    end = datetime.combine(day, time(hour, minute), timezone)
    return end - timedelta(days=1), end


def _as_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    timezone = ZoneInfo(_setting("daily_stats_timezone", "Europe/Moscow"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _in_window(value: object, start: datetime, end: datetime) -> bool:
    parsed = _as_datetime(value)
    return bool(parsed and start <= parsed < end)


def interval_data(report_date: date | str) -> dict[str, object]:
    start, end = report_bounds(report_date)
    rows = store.rows("SELECT * FROM crm_items")
    events = store.rows("SELECT * FROM crm_item_events ORDER BY occurred_at,id")
    histories: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        if _as_datetime(event["occurred_at"]):
            histories.setdefault((event["entity_type"], event["entity_id"]), []).append(event)

    snapshots = []
    for row in rows:
        history = histories.get((row["entity_type"], row["entity_id"]), [])
        report_stage_id = row["current_stage_id"]
        report_assignee_id = row["current_assignee_id"]
        # Rewind changes that happened at or after the cutoff. This keeps recovery
        # reports historically correct even if the CRM object has since moved on.
        for event in reversed(history):
            occurred_at = _as_datetime(event["occurred_at"])
            if not occurred_at or occurred_at < end:
                continue
            if event["event_type"] == "stage_changed":
                report_stage_id = event["old_value"]
            elif event["event_type"] in {"assignee_changed", "transferred"}:
                report_assignee_id = event["old_value"]
        snapshots.append({
            **row,
            "report_stage_id": report_stage_id,
            "report_assignee_id": report_assignee_id,
            "report_processed": bool(
                _as_datetime(row["processed_at"]) and _as_datetime(row["processed_at"]) < end
            ),
            "report_assignee_name": (
                row["current_assignee_name"]
                if str(row["current_assignee_id"] or "") == str(report_assignee_id or "") else None
            ),
        })
    qualified = [row for row in snapshots if _in_window(row["processed_at"], start, end)]
    new = [row for row in snapshots if _in_window(row["first_seen_at"], start, end)]
    rows_by_key = {(row["entity_type"], row["entity_id"]): row for row in rows}

    def is_transfer(event: dict) -> bool:
        if event["event_type"] == "transferred":
            return True
        if event["event_type"] != "assignee_changed":
            return False
        row = rows_by_key.get((event["entity_type"], event["entity_id"]))
        processed_at = _as_datetime(row["processed_at"]) if row else None
        occurred_at = _as_datetime(event["occurred_at"])
        # Compatibility with events recorded before the dedicated
        # "transferred" type existed. Strict comparison avoids treating the
        # assignee selected during qualification itself as a later transfer.
        return bool(processed_at and occurred_at and processed_at < occurred_at)

    pending = []
    for row in snapshots:
        first_seen = _as_datetime(row["first_seen_at"])
        processed_at = _as_datetime(row["processed_at"])
        unprocessed = (
            _setting("lead_unprocessed_status_id", "NEW")
            if row["entity_type"] == "lead"
            else _setting("deal_unprocessed_stage_id", "NEW")
        )
        if first_seen and first_seen < end and (not processed_at or processed_at >= end):
            if row["report_stage_id"] == unprocessed:
                pending.append(row)
    return {
        "start": start,
        "end": end,
        "new": new,
        "qualified": qualified,
        "transferred": [
            event for event in events
            if is_transfer(event) and _in_window(event["occurred_at"], start, end)
        ],
        "pending": pending,
    }


def build_daily_report(
    report_date: date | str,
    *,
    lead_status_map: dict[str, str] | None = None,
    deal_stage_map: dict[str, str] | None = None,
    user_map: dict[str, str] | None = None,
) -> str:
    day = report_date.isoformat() if isinstance(report_date, date) else report_date
    data = interval_data(day)
    start, end = data["start"], data["end"]
    rows = data["new"]
    processed = data["qualified"]
    transfers = data["transferred"]
    pending = data["pending"]
    leads = sum(row["entity_type"] == "lead" for row in rows)
    deals = sum(row["entity_type"] == "deal" for row in rows)

    def label(row: dict, name_key: str, id_key: str, empty: str) -> str:
        return str(row[name_key] or row[id_key] or empty)

    sources = Counter(label(row, "source_name", "source_id", "Без источника") for row in rows)
    managers = Counter(label(row, "processed_by_name", "processed_by_id", "Без ответственного") for row in processed)
    pending_managers = Counter(
        label(row, "report_assignee_name", "report_assignee_id", "Без ответственного")
        for row in pending
    )
    lead_status_map = lead_status_map or {}
    deal_stage_map = deal_stage_map or {}
    user_map = user_map or {}
    qualifications = Counter(
        (lead_status_map if row["entity_type"] == "lead" else deal_stage_map).get(
            str(row["report_stage_id"] or ""), str(row["report_stage_id"] or "Без стадии")
        )
        for row in processed
    )

    lines = [
        f"📊 <b>События за {start:%d.%m %H:%M} — {end:%d.%m %H:%M}</b>",
        f"Новые обращения: <b>{len(rows)}</b> (лиды: {leads}, сделки: {deals})",
        f"Квалифицировано: <b>{len(processed)}</b>",
        f"Передано: <b>{len(transfers)}</b>",
        f"Сейчас на стадии «Не обработан»: <b>{len(pending)}</b>",
    ]
    for title, values in (
        ("Источники новых", sources),
        ("Результат квалификации", qualifications),
        ("Квалифицировали", managers),
        ("Остаток по менеджерам", pending_managers),
    ):
        lines.append(f"\n<b>{title}</b>")
        lines.extend(f"• {escape(name)} — {count}" for name, count in values.most_common())
        if not values:
            lines.append("• нет")
    lines.append("\n<b>Передачи</b>")
    for event in transfers:
        entity = "Лид" if event["entity_type"] == "lead" else "Сделка"
        old_name = user_map.get(str(event["old_value"] or ""), str(event["old_value"] or "Без ответственного"))
        new_name = user_map.get(str(event["new_value"] or ""), str(event["new_value"] or "Без ответственного"))
        lines.append(
            f"• {entity} №{escape(str(event['entity_id']))}: "
            f"{escape(old_name)} → {escape(new_name)}"
        )
    if not transfers:
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
