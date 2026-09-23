"""Event-interval CRM reports for sales managers and directors."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app import stats, store
from app.bitrix_client import BitrixClient
from app.config import get_settings
from app.formatter import build_daily_report_body
from app.telegram_client import send_telegram_message

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _report_bounds(report_now: datetime) -> tuple[str, str]:
    start, end = stats.report_bounds(report_now.date())
    return start.isoformat(), end.isoformat()


def _group_leads_by_manager(leads: list[dict]) -> dict[str, list[dict]]:
    by_manager: dict[str, list[dict]] = {}
    tracked_by_id = {
        row["entity_id"]: row for row in store.rows(
            "SELECT entity_id,processed_at,processed_by_id,current_assignee_id "
            "FROM crm_items WHERE entity_type='lead'"
        )
    }
    for lead in leads:
        report_manager_id = str(lead.get("REPORT_ASSIGNED_BY_ID") or "")
        row = tracked_by_id.get(str(lead.get("ID") or ""))
        if report_manager_id:
            manager_id = report_manager_id
        elif row:
            manager_id = str(
                (row["processed_by_id"] if row["processed_at"] else row["current_assignee_id"]) or ""
            )
        else:
            manager_id = str(lead.get("ASSIGNED_BY_ID") or "")
        if manager_id:
            by_manager.setdefault(manager_id, []).append(lead)
    return by_manager


def _delivered(report_date: str, delivery_key: str) -> bool:
    return bool(store.rows(
        "SELECT 1 FROM daily_report_deliveries WHERE report_date=? AND chat_id=?",
        (report_date, delivery_key),
    ))


def _record_delivery(report_date: str, delivery_key: str, message: dict) -> None:
    store.execute(
        "INSERT INTO daily_report_deliveries VALUES (?,?,?) ON CONFLICT DO NOTHING",
        (report_date, delivery_key, message.get("message_id")),
    )


async def send_daily_reports(report_now: datetime | None = None) -> None:
    settings = get_settings()
    client = BitrixClient()
    report_now = report_now or datetime.now(MOSCOW_TZ)
    report_date = report_now.date().isoformat()

    start_iso, end_iso = _report_bounds(report_now)
    leads = await client.get_leads_created_between(start_iso, end_iso)
    interval = stats.interval_data(report_date)
    report_rows = {
        row["entity_id"]: row
        for key in ("new", "qualified")
        for row in interval[key]
        if row["entity_type"] == "lead"
    }
    for lead in leads:
        row = report_rows.get(str(lead.get("ID") or ""))
        if not row:
            continue
        lead["STATUS_ID"] = row["report_stage_id"]
        lead["REPORT_ASSIGNED_BY_ID"] = (
            row["processed_by_id"] if row["report_processed"] else row["report_assignee_id"]
        )
    lead_ids = {str(lead.get("ID") or "") for lead in leads}
    for row in interval["qualified"]:
        if row["entity_type"] != "lead" or row["entity_id"] in lead_ids:
            continue
        leads.append({
            "ID": row["entity_id"],
            "SOURCE_ID": row["source_id"],
            "STATUS_ID": row["report_stage_id"],
            "ASSIGNED_BY_ID": row["processed_by_id"],
            "REPORT_ASSIGNED_BY_ID": row["processed_by_id"],
        })
        lead_ids.add(row["entity_id"])
    sales_ids = {str(user["ID"]) for user in await client.get_department_users(settings.sales_department_id)}
    by_manager = {
        manager_id: manager_leads
        for manager_id, manager_leads in _group_leads_by_manager(leads).items()
        if manager_id in sales_ids
    }
    has_events = any(interval[key] for key in ("new", "qualified", "transferred"))
    if not by_manager and not has_events:
        logger.info("Daily report: no events in report interval, nothing to send")
        return

    source_map: dict[str, str] = {}
    status_map: dict[str, str] = {}
    lead_status_map: dict[str, str] = {}
    deal_stage_map: dict[str, str] = {}
    if by_manager or interval["qualified"]:
        sources = await client.get_sources()
        statuses = await client.get_lead_statuses()
        source_map = {str(item["STATUS_ID"]): item["NAME"] for item in sources}
        status_map = {str(item["STATUS_ID"]): item["NAME"] for item in statuses}
        lead_status_map = status_map
        deal_stages = await client.get_deal_stages(getattr(settings, "track_deal_category_id", "0"))
        deal_stage_map = {str(item["STATUS_ID"]): item["NAME"] for item in deal_stages}
    transfer_user_ids = {
        str(value)
        for event in interval["transferred"]
        for value in (event["old_value"], event["new_value"])
        if value
    }
    user_map = {
        user_id: await client.get_user_name(user_id) or user_id
        for user_id in transfer_user_ids
    }
    portal_domain = urlparse(settings.bitrix_webhook_url).netloc
    today_label = report_now.strftime("%d.%m.%Y")

    manager_blocks: list[tuple[str, str]] = []
    for manager_id, manager_leads in by_manager.items():
        body = build_daily_report_body(
            manager_leads, portal_domain=portal_domain, source_map=source_map, status_map=status_map,
        )
        name = await client.get_user_name(manager_id) or manager_id
        manager_blocks.append((name, body))

        manager_chat_id = store.manager_telegram_id(manager_id)
        delivery_key = f"manager:{manager_chat_id}"
        if manager_chat_id and not _delivered(report_date, delivery_key):
            text = f"📊 <b>Отчёт за {today_label}</b>\n\n{body}"
            try:
                message = await send_telegram_message(text, chat_id=manager_chat_id)
                _record_delivery(report_date, delivery_key, message)
            except Exception:
                logger.exception("Failed to send daily report to manager telegram_id=%s", manager_chat_id)

    manager_blocks.sort(key=lambda item: item[0])
    digest_text = f"📊 <b>Сводный отчёт за {today_label}</b>\n\n" + "\n\n──────────\n\n".join(
        f"👤 <b>{escape(name)}</b>\n\n{body}" for name, body in manager_blocks
    )
    if has_events:
        digest_text += "\n\n──────────\n\n" + stats.build_daily_report(
            report_date,
            lead_status_map=lead_status_map,
            deal_stage_map=deal_stage_map,
            user_map=user_map,
        )
    for director_id in settings.director_user_id_set:
        delivery_key = f"director:{director_id}"
        if _delivered(report_date, delivery_key):
            continue
        try:
            message = await send_telegram_message(digest_text, chat_id=director_id)
            _record_delivery(report_date, delivery_key, message)
        except Exception:
            logger.exception("Failed to send daily digest to director_id=%s", director_id)


async def daily_report_loop() -> None:
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            hour, minute = map(int, getattr(get_settings(), "daily_stats_time", "19:00").split(":"))
            report_day = now if now.time() >= time(hour, minute) else now - timedelta(days=1)
            await send_daily_reports(report_day)
        except Exception:
            logger.exception("Daily report generation failed")
        await asyncio.sleep(60)
