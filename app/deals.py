"""Tracking and Telegram management of deals copied into the sales pipeline."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from app import store
from app.bitrix_client import BitrixClient
from app.config import get_settings
from app.formatter import build_deal_manage_keyboard, build_deal_notification
from app.telegram_client import edit_message_text, send_telegram_message

logger = logging.getLogger(__name__)
lock = asyncio.Lock()
CURSOR_KEY = "sales_deal_cursor"


def destinations() -> list[str]:
    return [str(chat_id) for chat_id in get_settings().director_user_id_set]


def deliveries(deal_id: str) -> list[dict]:
    return store.rows("SELECT * FROM deal_deliveries WHERE deal_id=?", (str(deal_id),))


async def render(deal: dict, *, is_junk: bool = False) -> str:
    client = BitrixClient()
    contact = await client.get_contact(deal["CONTACT_ID"]) if deal.get("CONTACT_ID") else None
    source_name = await client.get_source_name(deal.get("SOURCE_ID", ""))
    assigned_name = await client.get_user_name(str(deal.get("ASSIGNED_BY_ID") or ""))
    exact_lead_id = str(deal.get("LEAD_ID") or "") or None
    related_lead_id = exact_lead_id
    relation_kind = "exact_seen" if exact_lead_id and store.was_lead_seen(exact_lead_id) else "exact" if exact_lead_id else None
    if not related_lead_id and contact:
        related_lead_id = store.find_seen_lead_by_phones(
            [(item or {}).get("VALUE") for item in contact.get("PHONE") or []]
        )
        if related_lead_id:
            relation_kind = "phone"
    return build_deal_notification(
        deal, contact=contact, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
        source_name=source_name, assigned_name=assigned_name, is_junk=is_junk,
        related_lead_id=related_lead_id, relation_kind=relation_kind,
    )


async def publish(deal_id: str) -> None:
    delivered = {row["chat_id"] for row in deliveries(deal_id)}
    pending = [chat_id for chat_id in destinations() if chat_id not in delivered]
    if not pending:
        store.execute("UPDATE deal_notifications SET dirty=0 WHERE deal_id=?", (deal_id,))
        return
    deal = await BitrixClient().get_deal(deal_id)
    text = await render(deal)
    for chat_id in pending:
        message = await send_telegram_message(
            text, reply_markup=build_deal_manage_keyboard(deal_id), chat_id=chat_id,
        )
        store.execute("INSERT OR IGNORE INTO deal_deliveries VALUES (?,?,?)", (deal_id, chat_id, message["message_id"]))
    store.execute("UPDATE deal_notifications SET dirty=0 WHERE deal_id=?", (deal_id,))


async def refresh_cards(deal: dict, *, is_junk: bool = False) -> None:
    text = await render(deal, is_junk=is_junk)
    for delivery in deliveries(str(deal["ID"])):
        await edit_message_text(
            delivery["chat_id"], delivery["message_id"], text,
            reply_markup=build_deal_manage_keyboard(deal["ID"]),
        )
    store.execute("UPDATE deal_notifications SET dirty=0 WHERE deal_id=?", (str(deal["ID"]),))


async def notify_assigned_manager(deal: dict, bitrix_user_id: str) -> None:
    manager_chat_id = store.manager_telegram_id(bitrix_user_id)
    if not manager_chat_id:
        return
    text = "📌 <b>На вас назначена сделка</b>\n\n" + await render(deal)
    await send_telegram_message(text, chat_id=manager_chat_id)


async def track(deal_id: str | int) -> bool:
    deal_id = str(deal_id)
    client = BitrixClient()
    deal = await client.get_deal(deal_id)
    if str(deal.get("CATEGORY_ID")) != get_settings().track_deal_category_id:
        return False
    store.execute("INSERT OR IGNORE INTO deal_notifications (deal_id) VALUES (?)", (deal_id,))
    await publish(deal_id)
    return True


async def _ensure_cursor() -> int:
    existing = store.rows("SELECT value FROM metadata WHERE key=?", (CURSOR_KEY,))
    if existing:
        return int(existing[0]["value"])
    latest = int(await BitrixClient().get_latest_deal_id(get_settings().track_deal_category_id))
    store.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (CURSOR_KEY, str(latest)))
    logger.info("Initialized sales deal cursor at %s", latest)
    return latest


async def poll_once() -> None:
    async with lock:
        for row in store.rows("SELECT deal_id FROM deal_notifications WHERE dirty=1"):
            try:
                await publish(row["deal_id"])
            except Exception:
                logger.exception("Failed to deliver deal_id=%s", row["deal_id"])
        cursor = await _ensure_cursor()
        deals = await BitrixClient().get_new_deals(get_settings().track_deal_category_id, cursor)
        for deal in deals:
            deal_id = str(deal["ID"])
            store.execute("INSERT OR IGNORE INTO deal_notifications (deal_id) VALUES (?)", (deal_id,))
            store.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (CURSOR_KEY, deal_id))
            try:
                await publish(deal_id)
            except Exception:
                logger.exception("Failed to deliver new deal_id=%s", deal_id)


async def polling_loop() -> None:
    while True:
        try:
            await poll_once()
        except Exception:
            logger.exception("Deal polling failed")
        await asyncio.sleep(max(5, get_settings().deal_poll_interval_seconds))
