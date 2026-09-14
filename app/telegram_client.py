"""Тонкий клиент для Telegram Bot API: уведомления + управление через inline-кнопки."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


async def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    return response.json()


async def send_telegram_message(text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = await _call("sendMessage", payload)
    return result["result"]


async def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await _call("editMessageText", payload)


async def edit_message_reply_markup(
    chat_id: int | str,
    message_id: int,
    reply_markup: dict[str, Any] | None,
) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await _call("editMessageReplyMarkup", payload)


async def answer_callback_query(
    callback_query_id: str,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text is not None:
        payload["text"] = text
    await _call("answerCallbackQuery", payload)
