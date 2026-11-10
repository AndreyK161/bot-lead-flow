"""Count contracts and CRM amounts by the contract's unified Source field."""
import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from apps.bot.backend.bitrix_client import BitrixClient
from apps.bot.backend.config import get_settings
from apps.bot.backend.outcomes import _contract_source_id


async def collect_contracts_report(manager_id: int, start: date, end: date) -> dict:
    settings = get_settings()
    tz = ZoneInfo(settings.daily_stats_timezone)
    lower = datetime.combine(start, time.min, tz)
    upper = datetime.combine(end + timedelta(days=1), time.min, tz)
    date_field = settings.deal_contract_date_field
    source_field = settings.contract_source_url_field
    category = settings.accompaniment_deal_category_id
    client = BitrixClient()
    contracts, sources, stages = await asyncio.gather(
        client.get_contracts_for_report(
            category, date_field, lower.isoformat(), upper.isoformat(), source_field=source_field,
        ),
        client.get_sources(),
        client.get_deal_stages(category),
    )
    failed_stages = {str(stage["STATUS_ID"]) for stage in stages if stage.get("SEMANTICS") == "F"}
    linked_ids = sorted({source_id for contract in contracts
                         if (source_id := _contract_source_id(contract, source_field))})
    sales = await client.get_deals_by_ids(linked_ids, select=["ID", "SOURCE_ID", "ASSIGNED_BY_ID"]) if linked_ids else []
    sales_by_id = {str(deal["ID"]): deal for deal in sales}
    names = {str(row["STATUS_ID"]): row.get("NAME") or str(row["STATUS_ID"])
             for row in sources if row.get("STATUS_ID")}
    groups = {}
    totals = defaultdict(Decimal)
    seen = set()
    for contract in contracts:
        sale = sales_by_id.get(_contract_source_id(contract, source_field))
        if (not sale or str(sale.get("ASSIGNED_BY_ID")) != str(manager_id)
                or str(contract.get("STAGE_ID")) in failed_stages):
            continue
        contract_id = str(contract["ID"])
        if contract_id in seen:
            continue
        raw_date = contract.get(date_field) or contract.get("DATE_CREATE")
        try:
            at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=tz)
        if not lower <= at < upper:
            continue
        seen.add(contract_id)
        source_id = str(sale.get("SOURCE_ID") or "").strip()
        group = groups.setdefault(source_id, {
            "name": names.get(source_id, source_id or "Без источника"),
            "count": 0, "revenue": defaultdict(Decimal), "missing_amount": 0,
        })
        group["count"] += 1
        currency = str(contract.get("CURRENCY_ID") or "").strip()
        try:
            amount = Decimal(str(contract.get("OPPORTUNITY")))
        except InvalidOperation:
            amount = None
        if not currency or amount is None or not amount.is_finite():
            group["missing_amount"] += 1
            continue
        group["revenue"][currency] += amount
        totals[currency] += amount

    def money(values):
        with localcontext() as context:
            context.rounding = ROUND_HALF_UP
            return {currency: format(amount, ".2f") for currency, amount in sorted(values.items())}

    rows = sorted(groups.values(), key=lambda item: (-item["count"], item["name"]))
    return {
        "count": len(seen), "revenue": money(totals),
        "sources": [{**group, "revenue": money(group["revenue"])} for group in rows],
        "missing_amount": sum(group["missing_amount"] for group in rows),
    }
