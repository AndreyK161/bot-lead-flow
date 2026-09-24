"""Тонкий клиент для Telegram Bot API: уведомления + управление через inline-кнопки."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import httpx

from app.config import get_settings


# Telegram accepts at most 4096 characters in a text message.  A small reserve
# also covers differences between raw HTML and the text counted by Telegram.
TELEGRAM_TEXT_LIMIT = 4000


class _TelegramHtmlSplitter(HTMLParser):
    """Split generated Telegram HTML while keeping every chunk valid HTML."""

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=False)
        self.limit = limit
        self.chunks: list[str] = []
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, str]] = []

    def _closers(self) -> str:
        return "".join(f"</{tag}>" for tag, _ in reversed(self.open_tags))

    def _reopeners(self) -> str:
        return "".join(raw for _, raw in self.open_tags)

    def _flush(self) -> None:
        if not self.parts:
            return
        chunk = "".join(self.parts) + self._closers()
        if chunk:
            self.chunks.append(chunk)
        reopeners = self._reopeners()
        self.parts = [reopeners] if reopeners else []

    def _available(self) -> int:
        return self.limit - len("".join(self.parts)) - len(self._closers())

    def _append_atomic(self, value: str) -> None:
        if len(value) > self._available() and self.parts:
            self._flush()
        self.parts.append(value)

    def _append_text(self, value: str) -> None:
        remaining = value
        while remaining:
            available = self._available()
            if available <= 0:
                self._flush()
                continue
            if len(remaining) <= available:
                self.parts.append(remaining)
                return

            split_at = max(
                remaining.rfind("\n", 0, available + 1),
                remaining.rfind(" ", 0, available + 1),
            )
            if split_at <= 0:
                split_at = available
            else:
                split_at += 1  # Keep the separator; no text is lost.
            self.parts.append(remaining[:split_at])
            remaining = remaining[split_at:]
            self._flush()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        closing = f"</{tag}>"
        if len(raw) + len(closing) > self._available() and self.parts:
            self._flush()
        self.parts.append(raw)
        self.open_tags.append((tag, raw))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append_atomic(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        for index in range(len(self.open_tags) - 1, -1, -1):
            if self.open_tags[index][0] == tag:
                del self.open_tags[index]
                break

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def handle_entityref(self, name: str) -> None:
        self._append_atomic(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append_atomic(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._append_atomic(f"<!--{data}-->")

    def finish(self) -> list[str]:
        if self.parts:
            self.chunks.append("".join(self.parts))
            self.parts = []
        return [chunk for chunk in self.chunks if chunk]


def split_telegram_html(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Return non-empty, independently valid HTML chunks within the limit."""
    if len(text) <= limit:
        return [text]
    splitter = _TelegramHtmlSplitter(limit)
    splitter.feed(text)
    splitter.close()
    return splitter.finish()


async def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            if method.startswith("editMessage") and "message is not modified" in response.text:
                return {"ok": True, "result": True}
            raise TelegramApiError(f"{method}: {response.status_code} {response.text}")
    return response.json()


class TelegramApiError(RuntimeError):
    """Telegram Bot API вернул ошибку — текст ответа содержит description с причиной."""


async def send_telegram_message(
    text: str,
    *,
    chat_id: int | str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_chat_id = chat_id if chat_id is not None else get_settings().telegram_chat_id
    chunks = split_telegram_html(text)
    last_message: dict[str, Any] = {}
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": target_chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None and index == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = await _call("sendMessage", payload)
        last_message = result["result"]
    return last_message


async def send_telegram_document(
    content: bytes,
    filename: str,
    *,
    chat_id: int | str,
    caption: str | None = None,
) -> dict[str, Any]:
    """Send an in-memory document without writing report files to disk."""
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    data: dict[str, Any] = {"chat_id": str(chat_id), "parse_mode": "HTML"}
    if caption:
        data["caption"] = caption
    files = {
        "document": (
            filename,
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, data=data, files=files)
        if response.status_code >= 400:
            raise TelegramApiError(f"sendDocument: {response.status_code} {response.text}")
    return response.json()["result"]


async def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    chunks = split_telegram_html(text)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": chunks[0],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if len(chunks) > 1:
        payload["reply_markup"] = {"inline_keyboard": []}
    elif reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await _call("editMessageText", payload)
    for index, chunk in enumerate(chunks[1:], start=1):
        follow_up: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None and index == len(chunks) - 1:
            follow_up["reply_markup"] = reply_markup
        await _call("sendMessage", follow_up)


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
