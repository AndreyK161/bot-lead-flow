"""Sales funnel conversion based on recorded deal stage changes."""
import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.bot.backend.bitrix_client import BitrixClient
from apps.bot.backend.config import get_settings


async def collect_sales_conversion(manager_id: int, start: date, end: date) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.daily_stats_timezone)
    lower = datetime.combine(start, time.min, tz)
    upper = datetime.combine(end + timedelta(days=1), time.min, tz)
    category = str(settings.track_deal_category_id)
    client = BitrixClient()
    activity, stages = await asyncio.gather(
        client.list_deal_stage_history({
            "CATEGORY_ID": category, ">=CREATED_TIME": lower.isoformat(),
            "<CREATED_TIME": upper.isoformat(),
        }),
        client.get_deal_stages(category),
    )
    candidates = sorted({str(row["OWNER_ID"]) for row in activity if row.get("OWNER_ID")})
    deals = await client.get_deals_by_ids(candidates, select=["ID", "ASSIGNED_BY_ID"]) if candidates else []
    ids = {str(deal["ID"]) for deal in deals if str(deal.get("ASSIGNED_BY_ID")) == str(manager_id)}
    history = await client.get_deal_stage_history(sorted(ids))
    events = defaultdict(list)
    for row in history:
        deal_id = str(row.get("OWNER_ID", ""))
        stage = str(row.get("STAGE_ID") or "")
        if deal_id not in ids or not stage:
            continue
        try:
            at = datetime.fromisoformat(str(row.get("CREATED_TIME") or "").replace("Z", "+00:00"))
            sequence = int(row.get("ID") or 0)
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=tz)
        if at < upper:
            events[deal_id].append((at, sequence, str(row.get("CATEGORY_ID")), stage))

    names = {
        str(row["STATUS_ID"]): row.get("NAME") or str(row["STATUS_ID"])
        for row in stages if row.get("STATUS_ID")
    }
    visits = defaultdict(set)
    moves = defaultdict(set)
    covered = set()
    for deal_id, rows in events.items():
        previous = None
        for at, _, event_category, stage in sorted(rows):
            if event_category != category:
                previous = None
                continue
            if at >= lower:
                covered.add(deal_id)
                visits[stage].add(deal_id)
                if previous and previous != stage:
                    visits[previous].add(deal_id)
                    moves[previous, stage].add(deal_id)
            previous = stage
    return {
        "deal_count": len(ids),
        "missing_history": len(ids - covered),
        "stages": [
            {"name": names.get(stage, stage), "count": len(visits[stage]), "transitions": [
                {"to": names.get(target, target), "count": len(deal_ids),
                 "percent": round(100 * len(deal_ids) / len(visits[stage]), 1)}
                for (source, target), deal_ids in sorted(moves.items()) if source == stage
            ]}
            for stage in dict.fromkeys([*names, *sorted(visits)])
        ],
    }
