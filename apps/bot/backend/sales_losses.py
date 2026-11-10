"""Break down recorded sales losses by the preceding stage and refusal reason."""
import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.bot.backend.bitrix_client import BitrixClient
from apps.bot.backend.config import get_settings
from apps.bot.backend.deal_rules import GENERIC_LOST_STAGES, REFUSAL_STAGE_REASONS
from apps.bot.backend.sales_duration import _timestamp


async def collect_sales_losses(manager_id: int, start: date, end: date) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.daily_stats_timezone)
    lower = datetime.combine(start, time.min, tz)
    upper = datetime.combine(end + timedelta(days=1), time.min, tz)
    category = str(settings.track_deal_category_id)
    client = BitrixClient()
    window = {
        "CATEGORY_ID": category, ">=CREATED_TIME": lower.isoformat(), "<CREATED_TIME": upper.isoformat(),
    }
    known_codes = set(REFUSAL_STAGE_REASONS) | GENERIC_LOST_STAGES
    prefix = "" if category == "0" else f"C{category}:"
    stages, failures, known_failures = await asyncio.gather(
        client.get_deal_stages(category),
        client.list_deal_stage_history({**window, "STAGE_SEMANTIC_ID": "F"}),
        client.list_deal_stage_history({**window, "@STAGE_ID": sorted(prefix + code for code in known_codes)}),
    )
    candidates = sorted({str(row["OWNER_ID"]) for row in [*failures, *known_failures] if row.get("OWNER_ID")})
    if not candidates:
        return {"count": 0, "stages": []}

    deals = await client.get_deals_by_ids(
        candidates, select=["ID", "ASSIGNED_BY_ID"],
    )
    owned = {str(deal["ID"]): deal for deal in deals if str(deal.get("ASSIGNED_BY_ID")) == str(manager_id)}
    history = await client.get_deal_stage_history(sorted(owned)) if owned else []
    names = {str(row["STATUS_ID"]): row.get("NAME") or str(row["STATUS_ID"])
             for row in stages if row.get("STATUS_ID")}

    by_deal = defaultdict(list)
    for event in history:
        owner = str(event.get("OWNER_ID"))
        at = _timestamp(event.get("CREATED_TIME"), tz)
        if owner in owned and at and at < upper and event.get("STAGE_ID"):
            by_deal[owner].append((at, int(event.get("ID") or 0), event))

    counts = defaultdict(Counter)
    for rows in by_deal.values():
        previous = None
        in_loss = False
        selected = None
        for at, _, event in sorted(rows, key=lambda row: row[:2]):
            if str(event.get("CATEGORY_ID")) != category:
                previous, in_loss = None, False
                continue
            stage = str(event["STAGE_ID"])
            code = stage.removeprefix(prefix) if prefix else stage
            failed = event.get("STAGE_SEMANTIC_ID") == "F" or code in known_codes
            if not failed:
                previous, in_loss = stage, False
                continue
            if not in_loss and at >= lower:
                selected = (previous, code)
            in_loss = True
        if selected is None:
            continue
        previous, code = selected
        reason = REFUSAL_STAGE_REASONS.get(code, "Историческая причина неизвестна")
        counts[previous][reason] += 1

    breakdown = [
        {
            "name": names.get(stage, stage or "Предыдущая стадия неизвестна"),
            "count": sum(reasons.values()),
            "reasons": [{"name": name, "count": count}
                        for name, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))],
        }
        for stage, reasons in counts.items()
    ]
    breakdown.sort(key=lambda row: (-row["count"], row["name"]))
    return {"count": sum(row["count"] for row in breakdown), "stages": breakdown}
