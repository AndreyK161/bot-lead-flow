"""Bitrix deal rules required for sales motivation reporting."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import stats, store
from app.bitrix_client import BitrixClient
from app.config import get_settings

logger = logging.getLogger(__name__)

# The sales funnel already has a separate stage for each concrete reason.
REFUSAL_STAGE_REASONS = {
    "UC_NPZOBZ": "Нет смысла звонить",
    "UC_O7XFI5": "Работает с другой компанией",
    "UC_1BEALQ": "Ушёл в игнор",
    "UC_8IETDG": "Брак",
    "UC_K0Z3P6": "Мусор",
}
GENERIC_LOST_STAGES = {"LOSE", "APOLOGY"}

_reason_item_cache: tuple[str, dict[str, str]] | None = None


def _label(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


async def _reason_item_ids(client: BitrixClient, field_name: str) -> dict[str, str]:
    global _reason_item_cache
    if _reason_item_cache and _reason_item_cache[0] == field_name:
        return _reason_item_cache[1]
    fields = await client.get_deal_userfields()
    field = next(
        (item for item in fields if str(item.get("FIELD_NAME") or "") == field_name), None,
    )
    if not field:
        raise RuntimeError(f"Bitrix deal field {field_name} is not configured")
    values = {
        _label(item.get("VALUE")): str(item.get("ID"))
        for item in (field.get("LIST") or [])
        if item.get("ID") and item.get("VALUE")
    }
    _reason_item_cache = (field_name, values)
    return values


def _previous_sales_stage(deal_id: str, current_stage: str) -> str:
    rows = store.rows(
        "SELECT current_stage_id FROM crm_items WHERE entity_type='deal' AND entity_id=?",
        (deal_id,),
    )
    previous = str(rows[0].get("current_stage_id") or "") if rows else ""
    if previous and previous != current_stage and previous not in GENERIC_LOST_STAGES:
        return previous
    return get_settings().deal_unprocessed_stage_id


async def apply_refusal_rule(deal: dict, client: BitrixClient, *, settings=None) -> dict:
    """Fill a reason from a refusal stage or reject a generic loss without one."""
    settings = settings or get_settings()
    if str(deal.get("CATEGORY_ID")) != settings.track_deal_category_id:
        return deal
    field_name = getattr(settings, "deal_refusal_reason_field", "UF_CRM_REFUSAL_REASON_OP")
    stage_id = str(deal.get("STAGE_ID") or "")
    current_reason = str(deal.get(field_name) or "")
    reason_name = REFUSAL_STAGE_REASONS.get(stage_id)
    if reason_name:
        item_ids = await _reason_item_ids(client, field_name)
        reason_id = item_ids.get(_label(reason_name))
        if not reason_id:
            raise RuntimeError(f"Bitrix refusal reason is missing: {reason_name}")
        if current_reason != reason_id:
            await client.update_deal(deal["ID"], {field_name: reason_id})
            deal = {**deal, field_name: reason_id}
        return deal
    if stage_id in GENERIC_LOST_STAGES and not current_reason:
        fallback = _previous_sales_stage(str(deal["ID"]), stage_id)
        logger.warning(
            "Rejecting deal loss without reason deal_id=%s stage=%s fallback=%s",
            deal["ID"], stage_id, fallback,
        )
        await client.update_deal(deal["ID"], {"STAGE_ID": fallback})
        return {**deal, "STAGE_ID": fallback}
    return deal


def _contract_timestamp(deal: dict, settings=None, *, created: bool) -> str:
    event_at = stats._as_datetime(
        deal.get("DATE_CREATE") if created else deal.get("DATE_MODIFY")
    )
    if event_at:
        return event_at.isoformat()
    settings = settings or get_settings()
    timezone = ZoneInfo(settings.daily_stats_timezone)
    return datetime.now(timezone).isoformat()


async def stamp_contract_date(
    deal: dict, client: BitrixClient, *, settings=None, created: bool = True,
) -> dict:
    """Persist the entry time for a newly-created category-2 deal."""
    settings = settings or get_settings()
    field_name = getattr(settings, "deal_contract_date_field", "UF_CRM_CONTRACT_DATE_OP")
    if (
        str(deal.get("CATEGORY_ID")) == getattr(settings, "accompaniment_deal_category_id", "2")
        and not deal.get(field_name)
    ):
        value = _contract_timestamp(deal, settings, created=created)
        await client.update_deal(deal["ID"], {field_name: value})
        store.execute(
            "UPDATE crm_items SET current_category_id=? "
            "WHERE entity_type='deal' AND entity_id=?",
            (str(deal.get("CATEGORY_ID") or ""), str(deal["ID"])),
        )
        return {**deal, field_name: value}
    return deal


async def apply_event_rules(
    deal: dict, client: BitrixClient, *, created: bool, settings=None,
) -> dict:
    settings = settings or get_settings()
    deal = await apply_refusal_rule(deal, client, settings=settings)
    category_id = str(deal.get("CATEGORY_ID") or "")
    accompaniment_id = getattr(settings, "accompaniment_deal_category_id", "2")
    tracked_before = bool(store.rows(
        "SELECT 1 FROM crm_items WHERE entity_type='deal' AND entity_id=? ",
        (str(deal.get("ID") or ""),),
    ))
    if category_id == accompaniment_id and (created or tracked_before):
        deal = await stamp_contract_date(
            deal, client, settings=settings, created=created,
        )
    return deal
