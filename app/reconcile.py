"""Periodic Bitrix-to-PostgreSQL reconciliation for missed webhook events."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import stats, store
from app.bitrix_client import BitrixClient
from app.config import get_settings

logger = logging.getLogger(__name__)
LEAD_CURSOR = "reconcile_lead_cursor"
DEAL_CURSOR = "reconcile_deal_cursor"


def _cursor(key: str, end: datetime) -> datetime:
    rows = store.rows("SELECT value FROM metadata WHERE key=?", (key,))
    if rows:
        try:
            value = datetime.fromisoformat(rows[0]["value"].replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Invalid reconciliation cursor key=%s value=%r", key, rows[0]["value"])
    return end - timedelta(hours=max(1, get_settings().reconcile_initial_lookback_hours))


def _save_cursor(key: str, value: datetime) -> None:
    store.execute(
        "INSERT INTO metadata VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value.isoformat()),
    )


async def reconcile_once(*, end: datetime | None = None) -> dict[str, int]:
    settings = get_settings()
    client = BitrixClient()
    end = end or datetime.now(timezone.utc)
    if not end.tzinfo:
        end = end.replace(tzinfo=timezone.utc)
    overlap = timedelta(seconds=max(0, settings.reconcile_overlap_seconds))
    counts = {"leads": 0, "deals": 0}

    lead_start = _cursor(LEAD_CURSOR, end) - overlap
    leads = await client.get_leads_modified_between(lead_start.isoformat(), end.isoformat())
    for lead in leads:
        await stats.observe("lead", lead, client)
        counts["leads"] += 1
    _save_cursor(LEAD_CURSOR, end)

    deal_start = _cursor(DEAL_CURSOR, end) - overlap
    deals = await client.get_deals_modified_between(
        settings.track_deal_category_id, deal_start.isoformat(), end.isoformat(),
    )
    for deal in deals:
        await stats.observe("deal", deal, client)
        counts["deals"] += 1
    _save_cursor(DEAL_CURSOR, end)

    logger.info(
        "Bitrix reconciliation complete leads=%s deals=%s end=%s",
        counts["leads"], counts["deals"], end.isoformat(),
    )
    return counts


async def reconciliation_loop() -> None:
    while True:
        try:
            await reconcile_once()
        except Exception:
            logger.exception("Bitrix reconciliation failed")
        await asyncio.sleep(max(30, get_settings().reconcile_interval_seconds))
