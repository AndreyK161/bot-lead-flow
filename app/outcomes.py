"""Daily current-outcome snapshots and compact period statistics."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from app import stats, store
from app.bitrix_client import DEAL_SELECT_FIELDS, BitrixClient
from app.config import get_settings

logger = logging.getLogger(__name__)
SYNC_KEY = "outcome_last_sync_at"
CONTRACT_CURSOR_KEY = "outcome_contract_deal_cursor"
CONTRACT_ID_RE = re.compile(r"/dogovor/(\d+)(?:/|\?|$)", re.IGNORECASE)
PERIOD_RE = re.compile(
    r"^/period(?:@\w+)?\s+(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*$",
    re.IGNORECASE,
)
lock = asyncio.Lock()


def _metadata(key: str) -> str | None:
    rows = store.rows("SELECT value FROM metadata WHERE key=?", (key,))
    return str(rows[0]["value"]) if rows else None


def _save_metadata(key: str, value: str) -> None:
    store.execute(
        "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _stage_map(items: list[dict]) -> dict[str, str]:
    return {str(item.get("STATUS_ID") or ""): str(item.get("NAME") or item.get("STATUS_ID") or "") for item in items}


def _normalize_title(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _deal_date(item: dict) -> datetime | None:
    return stats._as_datetime(item.get("DATE_CREATE"))


def _not_earlier(candidate: dict, source: dict) -> bool:
    candidate_date, source_date = _deal_date(candidate), _deal_date(source)
    return not candidate_date or not source_date or candidate_date >= source_date


def _contract_source_id(contract: dict, field: str) -> str | None:
    match = CONTRACT_ID_RE.search(str(contract.get(field) or ""))
    return match.group(1) if match else None


def _unique_index(items: list[dict], field: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        value = str(item.get(field) or "")
        if value and value != "0":
            grouped.setdefault(value, []).append(item)
    return {value: matches[0] for value, matches in grouped.items() if len(matches) == 1}


def _unique_title_index(items: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        title = _normalize_title(item.get("TITLE"))
        if title:
            grouped.setdefault(title, []).append(item)
    return {title: matches[0] for title, matches in grouped.items() if len(matches) == 1}


def _match_contracts(
    contracts: list[dict], sales_deals: list[dict], contract_field: str,
) -> dict[str, dict]:
    sales_by_id = {str(item["ID"]): item for item in sales_deals}
    sales_by_contact = _unique_index(sales_deals, "CONTACT_ID")
    sales_by_title = _unique_title_index(sales_deals)
    matched: dict[str, dict] = {}
    for contract in sorted(contracts, key=lambda item: int(item["ID"])):
        source = sales_by_id.get(_contract_source_id(contract, contract_field) or "")
        if not source:
            source = sales_by_contact.get(str(contract.get("CONTACT_ID") or ""))
        if not source:
            source = sales_by_title.get(_normalize_title(contract.get("TITLE")))
        if source and _not_earlier(contract, source):
            matched.setdefault(str(source["ID"]), contract)
    return matched


def _pick_related_deal(deals: list[dict], existing_id: object, contracts: dict[str, dict]) -> dict | None:
    by_id = {str(item["ID"]): item for item in deals}
    contracted = [item for item in deals if str(item["ID"]) in contracts]
    if contracted:
        return max(contracted, key=lambda item: int(item["ID"]))
    if str(existing_id or "") in by_id:
        return by_id[str(existing_id)]
    return max(deals, key=lambda item: int(item["ID"])) if deals else None


async def sync_once(*, now: datetime | None = None, client: BitrixClient | None = None) -> dict[str, int]:
    """Refresh every tracked item's current CRM outcome and persist human stage names."""
    async with lock:
        settings = get_settings()
        timezone = ZoneInfo(settings.daily_stats_timezone)
        checked_at = (now or datetime.now(timezone)).astimezone(timezone)
        client = client or BitrixClient()
        rows = store.rows("SELECT * FROM crm_items ORDER BY entity_type,entity_id")
        lead_rows = [row for row in rows if row["entity_type"] == "lead"]
        deal_rows = [row for row in rows if row["entity_type"] == "deal"]

        lead_statuses, sales_stages, accompaniment_stages = await asyncio.gather(
            client.get_lead_statuses(),
            client.get_deal_stages(settings.track_deal_category_id),
            client.get_deal_stages(settings.accompaniment_deal_category_id),
        )
        lead_names = _stage_map(lead_statuses)
        sales_names = _stage_map(sales_stages)
        accompaniment_names = _stage_map(accompaniment_stages)

        current_leads, direct_deals, related_deals = await asyncio.gather(
            client.get_leads_by_ids([row["entity_id"] for row in lead_rows]),
            client.get_deals_by_ids([row["entity_id"] for row in deal_rows]),
            client.get_deals_by_lead_ids(
                [row["entity_id"] for row in lead_rows], settings.track_deal_category_id,
            ),
        )
        contact_deals = await client.get_deals_by_contact_ids(
            [str(item.get("CONTACT_ID") or "") for item in current_leads],
            settings.track_deal_category_id,
        )
        current_leads_by_id = {str(item["ID"]): item for item in current_leads}
        sales_by_id = {
            str(item["ID"]): item
            for item in [*direct_deals, *related_deals, *contact_deals]
            if str(item.get("CATEGORY_ID", settings.track_deal_category_id)) == settings.track_deal_category_id
        }
        related_by_lead: dict[str, list[dict]] = {}
        for deal in sales_by_id.values():
            lead_id = str(deal.get("LEAD_ID") or "")
            if lead_id:
                related_by_lead.setdefault(lead_id, []).append(deal)
        deals_by_contact: dict[str, list[dict]] = {}
        for deal in sales_by_id.values():
            contact_id = str(deal.get("CONTACT_ID") or "")
            if contact_id and contact_id != "0":
                deals_by_contact.setdefault(contact_id, []).append(deal)
        leads_by_contact: dict[str, list[dict]] = {}
        for lead in current_leads:
            contact_id = str(lead.get("CONTACT_ID") or "")
            if contact_id and contact_id != "0":
                leads_by_contact.setdefault(contact_id, []).append(lead)
        for contact_id, matching_leads in leads_by_contact.items():
            if len(matching_leads) != 1:
                continue
            lead_id = str(matching_leads[0]["ID"])
            if not related_by_lead.get(lead_id):
                related_by_lead[lead_id] = deals_by_contact.get(contact_id, [])
        inferred_lead_by_deal: dict[str, str] = {}
        inferred_candidates: dict[str, list[str]] = {}
        for lead_id, matches in related_by_lead.items():
            for deal in matches:
                inferred_candidates.setdefault(str(deal["ID"]), []).append(lead_id)
        for deal_id, lead_ids in inferred_candidates.items():
            if len(set(lead_ids)) == 1:
                inferred_lead_by_deal[deal_id] = lead_ids[0]

        # Refresh the normal tracking fields and event history as part of the daily audit.
        for lead in current_leads:
            await stats.observe("lead", lead, client)
        for deal in sales_by_id.values():
            if str(deal["ID"]) in {row["entity_id"] for row in deal_rows}:
                await stats.observe("deal", deal, client)

        contract_field = settings.contract_source_url_field
        contract_cursor = int(_metadata(CONTRACT_CURSOR_KEY) or "0")
        new_contracts = await client.get_category_deals_after_id(
            settings.accompaniment_deal_category_id,
            contract_cursor,
            extra_fields=[contract_field],
        )
        known_contract_ids = list(dict.fromkeys(
            str(row["contract_deal_id"]) for row in rows if row.get("contract_deal_id")
        ))
        known_contracts = await client.get_deals_by_ids(
            known_contract_ids,
            select=list(dict.fromkeys(DEAL_SELECT_FIELDS + [contract_field])),
        )
        contracts_by_source = _match_contracts(
            [*known_contracts, *new_contracts], list(sales_by_id.values()), contract_field,
        )
        contracts_by_id = {
            str(item["ID"]): item for item in [*known_contracts, *new_contracts]
        }
        if new_contracts:
            _save_metadata(CONTRACT_CURSOR_KEY, str(max(int(item["ID"]) for item in new_contracts)))

        contracts = 0
        converted = 0
        for original in rows:
            original_id = str(original["entity_id"])
            if original["entity_type"] == "lead":
                current = current_leads_by_id.get(original_id)
                stage_id = str((current or {}).get("STATUS_ID") or original["current_stage_id"] or "")
                current_stage_name = lead_names.get(stage_id, stage_id or "Без стадии")
                category_id = None
                related = _pick_related_deal(
                    related_by_lead.get(original_id, []), original.get("linked_deal_id"), contracts_by_source,
                )
                if related:
                    converted += 1
            else:
                current = sales_by_id.get(original_id)
                stage_id = str((current or {}).get("STAGE_ID") or original["current_stage_id"] or "")
                current_stage_name = sales_names.get(stage_id, stage_id or "Без стадии")
                category_id = settings.track_deal_category_id
                related = current

            contract = contracts_by_source.get(str((related or {}).get("ID") or ""))
            if not contract and original.get("contract_deal_id"):
                contract = contracts_by_id.get(str(original["contract_deal_id"]))

            if contract:
                contracts += 1
                outcome_type = "deal"
                outcome_id = str(contract["ID"])
                outcome_category = settings.accompaniment_deal_category_id
                outcome_stage = str(contract.get("STAGE_ID") or "")
                outcome_name = accompaniment_names.get(outcome_stage, outcome_stage or "Без стадии")
                contract_id = outcome_id
                contract_at = original.get("contract_at") or str(contract.get("DATE_CREATE") or checked_at.isoformat())
            elif related:
                outcome_type = "deal"
                outcome_id = str(related["ID"])
                outcome_category = settings.track_deal_category_id
                outcome_stage = str(related.get("STAGE_ID") or "")
                outcome_name = sales_names.get(outcome_stage, outcome_stage or "Без стадии")
                contract_id = original.get("contract_deal_id")
                contract_at = original.get("contract_at")
            else:
                outcome_type = original["entity_type"]
                outcome_id = original_id
                outcome_category = category_id
                outcome_stage = stage_id
                outcome_name = current_stage_name
                contract_id = original.get("contract_deal_id")
                contract_at = original.get("contract_at")

            origin_lead_id = (
                str(
                    (current or {}).get("LEAD_ID")
                    or inferred_lead_by_deal.get(original_id)
                    or original.get("origin_lead_id")
                    or ""
                ) or None
                if original["entity_type"] == "deal" else original.get("origin_lead_id")
            )
            store.execute(
                """UPDATE crm_items SET current_stage_name=?,current_category_id=?,origin_lead_id=?,
                   linked_deal_id=?,outcome_entity_type=?,outcome_entity_id=?,outcome_category_id=?,
                   outcome_stage_id=?,outcome_stage_name=?,contract_deal_id=?,contract_at=?,
                   outcome_checked_at=? WHERE entity_type=? AND entity_id=?""",
                (
                    current_stage_name, category_id, origin_lead_id,
                    str(related["ID"]) if original["entity_type"] == "lead" and related else original.get("linked_deal_id"),
                    outcome_type, outcome_id, outcome_category, outcome_stage, outcome_name,
                    contract_id, contract_at, checked_at.isoformat(), original["entity_type"], original_id,
                ),
            )

        _save_metadata(SYNC_KEY, checked_at.isoformat())
        result = {"items": len(rows), "converted": converted, "contracts": contracts}
        logger.info("Daily outcome sync complete %s", result)
        return result


def parse_period(text: str) -> tuple[date, date] | None:
    match = PERIOD_RE.fullmatch(text.strip())
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%d.%m.%Y").date()
        end = datetime.strptime(match.group(2), "%d.%m.%Y").date()
    except ValueError:
        return None
    return (start, end) if start <= end else None


def build_period_report(start: date, end: date) -> str:
    rows = store.rows(
        """SELECT * FROM crm_items WHERE created_date>=? AND created_date<=?
           AND (entity_type='lead' OR origin_lead_id IS NULL OR origin_lead_id='')
           ORDER BY created_date,entity_type,entity_id""",
        (start.isoformat(), end.isoformat()),
    )
    label = f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"
    if not rows:
        return f"📊 <b>{label}</b>\nОбращений за период нет."

    lead_count = sum(row["entity_type"] == "lead" for row in rows)
    deal_count = len(rows) - lead_count
    converted = sum(bool(row.get("linked_deal_id")) for row in rows if row["entity_type"] == "lead")
    contracts = sum(bool(row.get("contract_deal_id")) for row in rows)
    outcomes: Counter[str] = Counter()
    for row in rows:
        if row.get("contract_deal_id"):
            outcome = "✅ Договор заключён"
        else:
            kind = "Сделка" if row.get("outcome_entity_type") == "deal" else "Лид"
            stage = str(row.get("outcome_stage_name") or row.get("current_stage_name") or row.get("current_stage_id") or "Не проверено")
            outcome = f"{kind} · {stage}"
        outcomes[outcome] += 1

    lines = [
        f"📊 <b>{label}</b>",
        f"Обращений: <b>{len(rows)}</b> · лиды: {lead_count} · прямые сделки: {deal_count}",
        f"Перешли в сделку: <b>{converted}</b> · договоры: <b>{contracts}</b>",
        "\n<b>Что с ними сейчас</b>",
    ]
    lines.extend(f"• {escape(name)} — {count}" for name, count in outcomes.most_common())
    last_sync = _metadata(SYNC_KEY)
    if last_sync:
        checked = stats._as_datetime(last_sync)
        if checked:
            lines.append(f"\n<i>Сверено с CRM: {checked:%d.%m.%Y %H:%M}</i>")
    return "\n".join(lines)


def _sync_due(now: datetime) -> bool:
    last_raw = _metadata(SYNC_KEY)
    if not last_raw:
        return True
    last = stats._as_datetime(last_raw)
    if not last:
        return True
    hour, minute = map(int, get_settings().outcome_sync_time.split(":"))
    return bool(last and last.date() < now.date() and now.time() >= time(hour, minute))


async def daily_sync_loop() -> None:
    while True:
        try:
            timezone = ZoneInfo(get_settings().daily_stats_timezone)
            now = datetime.now(timezone)
            if _sync_due(now):
                await sync_once(now=now)
        except Exception:
            logger.exception("Daily outcome sync failed")
        await asyncio.sleep(300)
