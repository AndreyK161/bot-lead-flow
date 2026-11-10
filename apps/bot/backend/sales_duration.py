"""Time from deal creation to the first successful stage entry."""
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from zoneinfo import ZoneInfo

from apps.bot.backend.bitrix_client import BitrixClient
from apps.bot.backend.config import get_settings


def _timestamp(value, tz: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(timezone.utc)


async def collect_sales_duration(manager_id: int, start: date, end: date) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.daily_stats_timezone)
    lower = datetime.combine(start, time.min, tz)
    upper = datetime.combine(end + timedelta(days=1), time.min, tz)
    category = str(settings.track_deal_category_id)
    client = BitrixClient()
    stages = await client.get_deal_stages(category)
    won_id = "WON" if category == "0" else f"C{category}:WON"
    if not any(stage.get("STATUS_ID") == won_id for stage in stages):
        matches = {stage["STATUS_ID"] for stage in stages if stage.get("STATUS_ID")
                   and str(stage.get("NAME", "")).strip().casefold() == "сделка успешна"}
        if len(matches) != 1:
            raise ValueError("В воронке «Продажа» не найдена однозначная успешная стадия")
        won_id = matches.pop()

    filters = {"CATEGORY_ID": category, "STAGE_ID": won_id, "<CREATED_TIME": upper.isoformat()}
    wins = await client.list_deal_stage_history({**filters, ">=CREATED_TIME": lower.isoformat()})
    candidates = sorted({str(row["OWNER_ID"]) for row in wins if row.get("OWNER_ID")})
    deals = await client.get_deals_by_ids(candidates, select=["ID", "DATE_CREATE", "ASSIGNED_BY_ID"]) if candidates else []
    owned = {str(deal["ID"]): deal for deal in deals if str(deal.get("ASSIGNED_BY_ID")) == str(manager_id)}
    first_wins = {}
    owned_ids = sorted(owned)
    for offset in range(0, len(owned_ids), 50):
        history = await client.list_deal_stage_history({**filters, "@OWNER_ID": owned_ids[offset:offset + 50]})
        for event in history:
            deal_id = str(event.get("OWNER_ID"))
            won = _timestamp(event.get("CREATED_TIME"), tz)
            if deal_id in owned and won and (deal_id not in first_wins or won < first_wins[deal_id]):
                first_wins[deal_id] = won

    details, durations = [], []
    missing_dates = 0
    for deal_id, won in sorted(first_wins.items(), key=lambda pair: pair[1], reverse=True):
        if not lower <= won < upper:
            continue
        created = _timestamp(owned[deal_id].get("DATE_CREATE"), tz)
        if created is None or created > won:
            missing_dates += 1
            continue
        days = (won - created).total_seconds() / 86400
        durations.append(days)
        details.append({
            "deal_id": deal_id, "created_at": created.astimezone(tz).isoformat(),
            "won_at": won.astimezone(tz).isoformat(), "days": round(days, 2),
        })
    return {
        "count": len(details), "missing_dates": missing_dates,
        "average_days": round(mean(durations), 2) if durations else None,
        "median_days": round(median(durations), 2) if durations else None,
        "deals": details,
    }
