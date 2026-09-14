"""FastAPI-сервис: приём лидов из Bitrix24 (ONCRMLEADADD), уведомления и управление в Telegram."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status

from app.bitrix_client import BitrixApiError, BitrixClient
from app.config import get_settings
from app.formatter import (
    build_action_keyboard,
    build_assign_keyboard,
    build_lead_notification,
    build_manage_keyboard,
)
from app.telegram_client import (
    answer_callback_query,
    edit_message_reply_markup,
    edit_message_text,
    send_telegram_message,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot-lead-flow")

app = FastAPI(title="bot-lead-flow")


def _portal_domain(webhook_url: str) -> str | None:
    """Достаёт домен портала из адреса входящего вебхука, чтобы собрать ссылку на лид."""
    host = urlparse(webhook_url).netloc
    return host or None


@app.post("/bitrix/webhook")
async def bitrix_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()

    # Bitrix шлёт form-urlencoded с вложенными ключами вида data[FIELDS][ID].
    form = await request.form()

    application_token = form.get("auth[application_token]")
    if application_token != settings.bitrix_application_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid application token")

    event = form.get("event")
    if event != "ONCRMLEADADD":
        # Не наш эндпоинт настроен на другое событие — просто игнорируем.
        return {"status": "ignored"}

    lead_id = form.get("data[FIELDS][ID]")
    if not lead_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing lead id")

    client = BitrixClient()

    try:
        lead = await client.get_lead(lead_id)

        source_name = None
        if lead.get("SOURCE_ID"):
            source_name = await client.get_source_name(lead["SOURCE_ID"])

        assigned_name = None
        if lead.get("ASSIGNED_BY_ID"):
            assigned_name = await client.get_user_name(lead["ASSIGNED_BY_ID"])
    except BitrixApiError:
        logger.exception("Bitrix API call failed for lead_id=%s", lead_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bitrix API error")

    message = build_lead_notification(
        lead,
        portal_domain=_portal_domain(settings.bitrix_webhook_url),
        source_name=source_name,
        assigned_name=assigned_name,
    )

    try:
        await send_telegram_message(message, reply_markup=build_manage_keyboard(lead_id))
    except Exception:
        logger.exception("Failed to send Telegram message for lead_id=%s", lead_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram API error")

    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret")

    update = await request.json()
    callback_query = update.get("callback_query")
    if not callback_query:
        return {"status": "ignored"}

    callback_id = callback_query["id"]
    from_user_id = callback_query["from"]["id"]

    if from_user_id != settings.admin_telegram_user_id:
        await answer_callback_query(callback_id, text="Нет доступа", show_alert=True)
        return {"status": "forbidden"}

    data = callback_query.get("data", "")
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    original_text = message.get("text", "")

    try:
        parts = data.split(":")
        action = parts[0]

        if action == "m":
            lead_id = parts[1]
            await edit_message_reply_markup(chat_id, message_id, build_action_keyboard(lead_id))
            await answer_callback_query(callback_id)

        elif action == "b":
            lead_id = parts[1]
            await edit_message_reply_markup(chat_id, message_id, build_manage_keyboard(lead_id))
            await answer_callback_query(callback_id)

        elif action == "a":
            lead_id = parts[1]
            client = BitrixClient()
            users = await client.get_department_users(settings.sales_department_id)
            await edit_message_reply_markup(chat_id, message_id, build_assign_keyboard(lead_id, users))
            await answer_callback_query(callback_id)

        elif action == "au":
            lead_id, user_id = parts[1], parts[2]
            client = BitrixClient()
            await client.update_lead(lead_id, {"ASSIGNED_BY_ID": user_id})
            assigned_name = await client.get_user_name(user_id)
            new_text = f"{original_text}\n\n✅ Назначен: {assigned_name or user_id}"
            await edit_message_text(chat_id, message_id, new_text, reply_markup=build_manage_keyboard(lead_id))
            await answer_callback_query(callback_id, text="Ответственный назначен")

        elif action == "j":
            lead_id = parts[1]
            client = BitrixClient()
            await client.update_lead(lead_id, {"STATUS_ID": settings.junk_status_id})
            new_text = f"{original_text}\n\n🗑 Перенесён в «Мусор»"
            await edit_message_text(chat_id, message_id, new_text, reply_markup=build_manage_keyboard(lead_id))
            await answer_callback_query(callback_id, text="Лид перенесён в мусор")

        else:
            await answer_callback_query(callback_id)

    except BitrixApiError:
        logger.exception("Bitrix API call failed while handling callback data=%s", data)
        await answer_callback_query(callback_id, text="Ошибка Bitrix API", show_alert=True)
    except Exception:
        logger.exception("Failed to handle Telegram callback data=%s", data)
        await answer_callback_query(callback_id, text="Внутренняя ошибка", show_alert=True)

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
