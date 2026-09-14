"""FastAPI-сервис: приём лидов из Bitrix24 (ONCRMLEADADD) и уведомления в Telegram."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request, status

from app.bitrix_client import BitrixApiError, BitrixClient
from app.config import get_settings
from app.formatter import build_lead_notification
from app.telegram_client import send_telegram_message

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
        await send_telegram_message(message)
    except Exception:
        logger.exception("Failed to send Telegram message for lead_id=%s", lead_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram API error")

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
