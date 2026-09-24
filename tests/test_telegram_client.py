import unittest
from html.parser import HTMLParser
from unittest.mock import AsyncMock, patch

from app.telegram_client import (
    TELEGRAM_TEXT_LIMIT,
    edit_message_text,
    send_telegram_message,
    set_chat_menu_button,
    split_telegram_html,
)


class _StrictHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


class TelegramMessageSplittingTests(unittest.IsolatedAsyncioTestCase):
    def test_short_message_is_not_changed(self):
        self.assertEqual(split_telegram_html("Привет <b>мир</b>"), ["Привет <b>мир</b>"])

    def test_long_html_is_split_into_valid_chunks(self):
        text = "<b>Отчёт</b>\n" + "".join(
            f'<a href="https://example.test/{index}">Лид №{index}</a>, '
            for index in range(500)
        )

        chunks = split_telegram_html(text)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks))
        for chunk in chunks:
            parser = _StrictHtmlParser()
            parser.feed(chunk)
            self.assertEqual(parser.stack, [])

    def test_long_text_inside_tag_is_closed_and_reopened(self):
        chunks = split_telegram_html("<code>" + "x" * 9000 + "</code>")

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(chunk.startswith("<code>") and chunk.endswith("</code>") for chunk in chunks))
        self.assertTrue(all(len(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks))

    async def test_send_uses_last_chunk_for_keyboard_and_return_value(self):
        call = AsyncMock(side_effect=[
            {"result": {"message_id": 10}},
            {"result": {"message_id": 11}},
        ])
        keyboard = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}
        with patch("app.telegram_client._call", new=call):
            result = await send_telegram_message("x" * 5000, chat_id=123, reply_markup=keyboard)

        self.assertEqual(result["message_id"], 11)
        self.assertNotIn("reply_markup", call.await_args_list[0].args[1])
        self.assertEqual(call.await_args_list[1].args[1]["reply_markup"], keyboard)

    async def test_long_edit_sends_followups_and_moves_keyboard(self):
        call = AsyncMock(return_value={"result": {"message_id": 11}})
        keyboard = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}
        with patch("app.telegram_client._call", new=call):
            await edit_message_text(123, 10, "x" * 5000, reply_markup=keyboard)

        self.assertEqual(call.await_args_list[0].args[0], "editMessageText")
        self.assertEqual(call.await_args_list[0].args[1]["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(call.await_args_list[1].args[0], "sendMessage")
        self.assertEqual(call.await_args_list[1].args[1]["reply_markup"], keyboard)

    async def test_sets_native_web_app_menu_button_for_requested_chat(self):
        call = AsyncMock(return_value={"ok": True, "result": True})
        with patch("app.telegram_client._call", new=call):
            await set_chat_menu_button(
                "https://lead.prav-buro.ru/miniapp",
                chat_id=1297686797,
            )

        call.assert_awaited_once_with("setChatMenuButton", {
            "chat_id": 1297686797,
            "menu_button": {
                "type": "web_app",
                "text": "Отчёты",
                "web_app": {"url": "https://lead.prav-buro.ru/miniapp"},
            },
        })


if __name__ == "__main__":
    unittest.main()
