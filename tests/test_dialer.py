import asyncio
import tempfile
import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.bot.backend import dialer, store


class DialerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/lead-flow.sqlite3",
            database_url="",
            daily_stats_timezone="Europe/Moscow",
            bitrix_webhook_url="https://portal.example/rest/1/token/",
            megafon_vats_api_url="https://vats.example/crmapi/v1",
            megafon_vats_api_key="secret",
            megafon_vats_auth_mode="header",
            megafon_vats_auth_header="X-API-KEY",
            megafon_dialer_user_map={"110": {"user": "701", "group": "", "clid": "74950000000"}},
        )
        self.store_patch = patch("apps.bot.backend.store.get_settings", return_value=self.settings)
        self.settings_patch = patch("apps.bot.backend.dialer.get_settings", return_value=self.settings)
        self.store_patch.start(); self.settings_patch.start()

    async def asyncTearDown(self):
        self.settings_patch.stop(); self.store_patch.stop(); self.tmp.cleanup()

    async def create_queue(self, telegram_id=100):
        client = AsyncMock()
        client.get_dialer_leads.return_value = [{
            "ID": "42", "TITLE": "Лид 42", "NAME": "Анна", "LAST_NAME": "Иванова",
            "PHONE": [{"VALUE": "+7 (999) 123-45-67"}], "STATUS_ID": "IN_PROCESS",
        }, {
            "ID": "43", "TITLE": "Без телефона", "STATUS_ID": "IN_PROCESS",
        }]
        client.get_lead_statuses.return_value = [{"STATUS_ID": "IN_PROCESS", "NAME": "В работе"}]
        client.get_contacts_full_by_ids.return_value = []
        with patch("apps.bot.backend.dialer.BitrixClient", return_value=client):
            return await dialer.create_session(
                telegram_id=telegram_id, bitrix_user_id="110", manager_name="Анна",
                entity_type="lead", date_from=date(2026, 10, 29), date_to=date(2026, 10, 29),
                category_id=None, stage_ids=["IN_PROCESS"], auto_advance=True,
            )

    async def test_concurrent_requests_create_only_one_active_queue(self):
        # Initialize before starting workers, as the application does at startup.
        store.rows("SELECT id FROM dialer_sessions")
        # Separate event loops and DB connections model independent workers.
        both_read_empty = threading.Barrier(2)
        async def fetch_leads(*args):
            await asyncio.to_thread(both_read_empty.wait, 5)
            return [{"ID": "42", "TITLE": "Test lead", "PHONE": [{"VALUE": "+79991234567"}]}]
        client = AsyncMock()
        client.get_dialer_leads.side_effect = fetch_leads
        client.get_lead_statuses.return_value = []
        client.get_contacts_full_by_ids.return_value = []
        async def create():
            return await dialer.create_session(
                telegram_id=100, bitrix_user_id="110", manager_name="Test manager",
                entity_type="lead", date_from=date(2026, 10, 29), date_to=date(2026, 10, 29),
                category_id=None, stage_ids=[], auto_advance=True,
            )
        with patch("apps.bot.backend.dialer.BitrixClient", return_value=client):
            results = await asyncio.gather(
                asyncio.to_thread(lambda: asyncio.run(create())),
                asyncio.to_thread(lambda: asyncio.run(create())),
                return_exceptions=True,
            )
        queues = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(queues), 1, results)
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], dialer.DialerError)
        self.assertEqual(errors[0].status, 409)
        self.assertEqual(len(store.rows("SELECT id FROM dialer_sessions WHERE telegram_id=? AND status IN ('active','paused')", (100,))), 1)
        self.assertEqual(len(store.rows("SELECT id FROM dialer_queue_items")), 1)
        # Cancelling releases the slot; a persistent guard row must not block it.
        dialer.cancel(queues[0]["session"]["id"], 100)
        replacement = await self.create_queue()
        self.assertEqual(replacement["session"]["status"], "active")

    async def test_paused_queue_blocks_creation_until_cancelled(self):
        payload = await self.create_queue()
        store.execute("UPDATE dialer_sessions SET status='paused' WHERE id=?", (payload["session"]["id"],))
        with self.assertRaises(dialer.DialerError) as error:
            await self.create_queue()
        self.assertEqual(error.exception.status, 409)
        dialer.cancel(payload["session"]["id"], 100)
        self.assertEqual((await self.create_queue())["session"]["status"], "active")

    async def test_another_user_can_create_queue_while_first_is_active(self):
        first = await self.create_queue(100)
        second = await self.create_queue(200)
        self.assertNotEqual(first["session"]["id"], second["session"]["id"])
        self.assertEqual(second["session"]["status"], "active")

    async def test_queue_is_durable_and_skips_entities_without_phone(self):
        payload = await self.create_queue()
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["current"]["phone"], "79991234567")
        self.assertEqual(payload["current"]["stage_name"], "В работе")
        self.assertEqual(dialer.current_session(100)["session"]["id"], payload["session"]["id"])

    async def test_start_and_success_callback_wait_for_manager_confirmation(self):
        payload = await self.create_queue()
        with patch("apps.bot.backend.dialer.make_call", AsyncMock(return_value="CALL-42")):
            started = await dialer.start_next(payload["session"]["id"], 100)
        self.assertEqual(started["current"]["status"], "calling")
        handled = await dialer.process_callback({
            "cmd": "history", "callid": "CALL-42", "status": "Success", "duration": "91",
        })
        self.assertTrue(handled)
        current = dialer.current_session(100)
        self.assertEqual(current["session"]["status"], "paused")
        self.assertEqual(current["current"]["status"], "awaiting_confirmation")
        resolved = await dialer.resolve(current["session"]["id"], current["current"]["id"], "answered", 100)
        self.assertEqual(resolved["summary"]["answered"], 1)

    async def test_missed_callback_is_idempotent_and_completes_queue(self):
        payload = await self.create_queue()
        with patch("apps.bot.backend.dialer.make_call", AsyncMock(return_value="CALL-42")):
            await dialer.start_next(payload["session"]["id"], 100)
        comment = AsyncMock()
        with patch("apps.bot.backend.dialer._comment", comment):
            await dialer.process_callback({
                "cmd": "history", "callid": "CALL-42", "status": "missed", "duration": 0,
            })
            await dialer.process_callback({
                "cmd": "history", "callid": "CALL-42", "status": "missed", "duration": 0,
            })
        current = dialer.current_session(100)
        self.assertEqual(current["session"]["status"], "completed")
        self.assertEqual(current["summary"]["failed"], 1)
        self.assertEqual(len(store.rows("SELECT * FROM dialer_call_events")), 1)
        comment.assert_awaited_once()

    async def test_session_cannot_be_read_by_another_manager(self):
        payload = await self.create_queue()
        with self.assertRaises(dialer.DialerError) as error:
            dialer.session_payload(payload["session"]["id"], 200)
        self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
