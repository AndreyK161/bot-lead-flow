"""Ежедневный отчёт по лидам, созданным за день — каждому продажнику свой, руководителям сводный."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app import store
from app.bitrix_client import BitrixClient
from app.config import get_settings
from app.formatter import build_daily_report_body
from app.telegram_client import send_telegram_message

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
REPORT_HOUR = 19


def _today_bounds_moscow(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(MOSCOW_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _group_leads_by_manager(leads: list[dict]) -> dict[str, list[dict]]:
    by_manager: dict[str, list[dict]] = {}
    for lead in leads:
        manager_id = str(lead.get("ASSIGNED_BY_ID") or "")
        if manager_id:
            by_manager.setdefault(manager_id, []).append(lead)
    return by_manager


async def send_daily_reports() -> None:
    settings = get_settings()
    client = BitrixClient()

    start_iso, end_iso = _today_bounds_moscow()
    leads = await client.get_leads_created_between(start_iso, end_iso)
    by_manager = _group_leads_by_manager(leads)
    if not by_manager:
        logger.info("Daily report: no leads created today, nothing to send")
        return

    sources = await client.get_sources()
    statuses = await client.get_lead_statuses()
    source_map = {str(item["STATUS_ID"]): item["NAME"] for item in sources}
    status_map = {str(item["STATUS_ID"]): item["NAME"] for item in statuses}
    portal_domain = urlparse(settings.bitrix_webhook_url).netloc
    today_label = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")

    manager_blocks: list[tuple[str, str]] = []
    for manager_id, manager_leads in by_manager.items():
        body = build_daily_report_body(
            manager_leads, portal_domain=portal_domain, source_map=source_map, status_map=status_map,
        )
        name = await client.get_user_name(manager_id) or manager_id
        manager_blocks.append((name, body))

        manager_chat_id = store.manager_telegram_id(manager_id)
        if manager_chat_id:
            text = f"📊 <b>Отчёт за {today_label}</b>\n\n{body}"
            try:
                await send_telegram_message(text, chat_id=manager_chat_id)
            except Exception:
                logger.exception("Failed to send daily report to manager telegram_id=%s", manager_chat_id)

    manager_blocks.sort(key=lambda item: item[0])
    digest_text = f"📊 <b>Сводный отчёт за {today_label}</b>\n\n" + "\n\n──────────\n\n".join(
        f"👤 <b>{escape(name)}</b>\n\n{body}" for name, body in manager_blocks
    )
    for director_id in settings.director_user_id_set:
        try:
            await send_telegram_message(digest_text, chat_id=director_id)
        except Exception:
            logger.exception("Failed to send daily digest to director_id=%s", director_id)


async def daily_report_loop() -> None:
    while True:
        now = datetime.now(MOSCOW_TZ)
        target = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_daily_reports()
        except Exception:
            logger.exception("Daily report generation failed")
