"""Тонкий клиент для Telegram Bot API: уведомления + управление через inline-кнопки."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


async def _call(method: str, payload: dict[str, Any], *, manual: bool = False) -> dict[str, Any]:
    settings = get_settings()
    token = settings.manual_bot_token if manual else settings.telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            if method.startswith('editMessage') and 'message is not modified' in response.text:
                return {'ok': True, 'result': True}
            raise TelegramApiError(f"{method}: {response.status_code} {response.text}")
    return response.json()


class TelegramApiError(RuntimeError):
    """Telegram Bot API вернул ошибку — текст ответа содержит description с причиной."""


async def send_telegram_message(text: str, *, reply_markup: dict[str, Any] | None = None, chat_id: str | int | None = None) -> dict[str, Any]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    destinations = [chat_id] if chat_id is not None else settings.notification_chat_ids
    first = None
    for destination in destinations:
        payload['chat_id'] = destination
        result = await _call("sendMessage", payload)
        if first is None:
            first = result['result']
    return first


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
