import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import dialer, store


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
        self.store_patch = patch("app.store.get_settings", return_value=self.settings)
        self.settings_patch = patch("app.dialer.get_settings", return_value=self.settings)
        self.store_patch.start(); self.settings_patch.start()

    async def asyncTearDown(self):
        self.settings_patch.stop(); self.store_patch.stop(); self.tmp.cleanup()

    async def create_queue(self):
        client = AsyncMock()
        client.get_dialer_leads.return_value = [{
            "ID": "42", "TITLE": "Лид 42", "NAME": "Анна", "LAST_NAME": "Иванова",
            "PHONE": [{"VALUE": "+7 (999) 123-45-67"}], "STATUS_ID": "IN_PROCESS",
        }, {
            "ID": "43", "TITLE": "Без телефона", "STATUS_ID": "IN_PROCESS",
        }]
        client.get_lead_statuses.return_value = [{"STATUS_ID": "IN_PROCESS", "NAME": "В работе"}]
        client.get_contacts_full_by_ids.return_value = []
        with patch("app.dialer.BitrixClient", return_value=client):
            return await dialer.create_session(
                telegram_id=100, bitrix_user_id="110", manager_name="Анна",
                entity_type="lead", date_from=date(2026, 10, 29), date_to=date(2026, 10, 29),
                category_id=None, stage_ids=["IN_PROCESS"], auto_advance=True,
            )

    async def test_queue_is_durable_and_skips_entities_without_phone(self):
        payload = await self.create_queue()
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["current"]["phone"], "79991234567")
        self.assertEqual(payload["current"]["stage_name"], "В работе")
        self.assertEqual(dialer.current_session(100)["session"]["id"], payload["session"]["id"])

    async def test_start_and_success_callback_wait_for_manager_confirmation(self):
        payload = await self.create_queue()
        with patch("app.dialer.make_call", AsyncMock(return_value="CALL-42")):
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
        with patch("app.dialer.make_call", AsyncMock(return_value="CALL-42")):
            await dialer.start_next(payload["session"]["id"], 100)
        comment = AsyncMock()
        with patch("app.dialer._comment", comment):
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
