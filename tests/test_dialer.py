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

    async def test_old_callbacks_cannot_replace_a_retried_call_or_advance_queue(self):
        events = (
            {'cmd': 'history', 'status': 'missed'},
            {'cmd': 'history', 'status': 'Success'},
            {'cmd': 'event', 'type': 'ACCEPTED'},
        )
        for index, event in enumerate(events):
            with self.subTest(event=event):
                user_id = 500 + index
                payload = await self.create_queue(user_id)
                session_id = payload['session']['id']
                item_id = payload['current']['id']
                store.execute("INSERT INTO dialer_queue_items(session_id,position,entity_type,entity_id,phone) VALUES(?,2,'lead','99','79990000001')", (session_id,))
                old_call, new_call = f'OLD-{index}', f'NEW-{index}'
                with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(side_effect=[old_call, new_call])) as calls, \
                     patch('apps.bot.backend.dialer._comment', new=AsyncMock()):
                    await dialer.start_next(session_id, user_id)
                    await dialer.resolve(session_id, item_id, 'retry', user_id)
                    self.assertEqual(calls.await_count, 2)
                before = dialer.session_payload(session_id, user_id)
                with patch('apps.bot.backend.dialer.make_call', new=AsyncMock()) as call, \
                     patch('apps.bot.backend.dialer._comment', new=AsyncMock()) as comment:
                    self.assertTrue(await dialer.process_callback({**event, 'callid': old_call}))
                call.assert_not_awaited()
                comment.assert_not_awaited()
                self.assertEqual(dialer.session_payload(session_id, user_id), before)
                attempts = store.rows('SELECT call_id,status FROM dialer_call_attempts WHERE item_id=? ORDER BY id', (item_id,))
                self.assertEqual(attempts, [{'call_id': old_call, 'status': 'completed'}, {'call_id': new_call, 'status': 'calling'}])

    async def test_changed_history_delivery_does_not_override_manual_confirmation(self):
        payload = await self.create_queue()
        session_id, item_id = payload['session']['id'], payload['current']['id']
        with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='CONFIRMED')):
            await dialer.start_next(session_id, 100)
        with patch('apps.bot.backend.dialer._comment', new=AsyncMock()):
            await dialer.process_callback({'cmd': 'history', 'callid': 'CONFIRMED', 'status': 'Success', 'duration': 90})
            await dialer.resolve(session_id, item_id, 'answered', 100)
        before = dialer.session_payload(session_id, 100)
        with patch('apps.bot.backend.dialer._comment', new=AsyncMock()) as comment:
            await dialer.process_callback({'cmd': 'history', 'callid': 'CONFIRMED', 'status': 'Success', 'duration': 91})
        comment.assert_not_awaited()
        self.assertEqual(dialer.session_payload(session_id, 100), before)

    async def test_late_callback_does_not_restart_cancelled_queue(self):
        for index, status in enumerate(('missed', 'Success')):
            with self.subTest(status=status):
                payload = await self.create_queue(100 + index)
                session_id = payload['session']['id']
                store.execute("INSERT INTO dialer_queue_items(session_id,position,entity_type,entity_id,phone) VALUES(?,2,'lead','99','79990000001')", (session_id,))
                call_id = f'CALL-CANCELLED-{index}'
                with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value=call_id)):
                    await dialer.start_next(session_id, 100 + index)
                dialer.cancel(session_id, 100 + index)
                with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value=f'UNEXPECTED-{index}')) as call, \
                     patch('apps.bot.backend.dialer._comment', new=AsyncMock()):
                    self.assertTrue(await dialer.process_callback({'cmd': 'history', 'callid': call_id, 'status': status}))
                call.assert_not_awaited()
                self.assertEqual(dialer.session_payload(session_id, 100 + index)['session']['status'], 'cancelled')

    async def test_cancellation_between_read_and_claim_prevents_a_new_call(self):
        payload = await self.create_queue()
        session_id = payload['session']['id']
        original_rows = store.rows
        def cancel_after_read(sql, params=()):
            rows = original_rows(sql, params)
            if "status IN ('pending','error')" in sql and 'dialer_queue_items' in sql:
                dialer.cancel(session_id, 100)
            return rows
        with patch('apps.bot.backend.dialer.store.rows', side_effect=cancel_after_read), \
             patch('apps.bot.backend.dialer.make_call', new=AsyncMock()) as call:
            with self.assertRaises(dialer.DialerError) as error:
                await dialer.start_next(session_id, 100)
        self.assertEqual(error.exception.status, 409)
        call.assert_not_awaited()
        self.assertEqual(dialer.session_payload(session_id, 100)['session']['status'], 'cancelled')
        self.assertEqual(store.rows('SELECT count(*) AS total FROM dialer_call_attempts')[0]['total'], 0)

    async def test_result_cannot_reopen_a_cancelled_queue(self):
        payload = await self.create_queue()
        session_id = payload['session']['id']
        dialer.cancel(session_id, 100)
        with patch('apps.bot.backend.dialer._comment', new=AsyncMock()):
            with self.assertRaises(dialer.DialerError) as error:
                await dialer.resolve(session_id, payload['current']['id'], 'failed', 100)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(dialer.session_payload(session_id, 100)['session']['status'], 'cancelled')

    async def test_stale_tab_cannot_finish_queue_with_another_call_running(self):
        payload = await self.create_queue()
        session_id, item_id = payload['session']['id'], payload['current']['id']
        store.execute("INSERT INTO dialer_queue_items(session_id,position,entity_type,entity_id,phone) VALUES(?,2,'lead','99','79990000001')", (session_id,))
        with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='FIRST')):
            await dialer.start_next(session_id, 100)
        await dialer.process_callback({'cmd': 'history', 'callid': 'FIRST', 'status': 'Success'})
        with patch('apps.bot.backend.dialer._comment', new=AsyncMock()), \
             patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='SECOND')):
            await dialer.resolve(session_id, item_id, 'failed', 100)
        before = dialer.session_payload(session_id, 100)
        with patch('apps.bot.backend.dialer._comment', new=AsyncMock()) as comment, \
             patch('apps.bot.backend.dialer.make_call', new=AsyncMock()) as call:
            with self.assertRaises(dialer.DialerError) as error:
                await dialer.resolve(session_id, item_id, 'answered', 100)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(dialer.session_payload(session_id, 100), before)
        comment.assert_not_awaited()
        call.assert_not_awaited()

    async def test_result_changed_between_read_and_write_is_rejected(self):
        payload = await self.create_queue()
        session_id, item_id = payload['session']['id'], payload['current']['id']
        with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='FIRST')):
            await dialer.start_next(session_id, 100)
        await dialer.process_callback({'cmd': 'history', 'callid': 'FIRST', 'status': 'Success'})
        original = store.rows
        def changed_after_read(sql, params=()):
            rows = original(sql, params)
            if 'WHERE id=? AND session_id=?' in sql:
                store.execute("UPDATE dialer_queue_items SET status='no_answer',last_result='failed' WHERE id=?", (item_id,))
            return rows
        with patch('apps.bot.backend.dialer.store.rows', side_effect=changed_after_read), \
             patch('apps.bot.backend.dialer._comment', new=AsyncMock()) as comment:
            with self.assertRaises(dialer.DialerError) as error:
                await dialer.resolve(session_id, item_id, 'answered', 100)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(original('SELECT status FROM dialer_queue_items WHERE id=?', (item_id,))[0]['status'], 'no_answer')
        comment.assert_not_awaited()

    async def test_concurrent_manual_results_only_one_can_be_saved(self):
        payload = await self.create_queue()
        session_id, item_id = payload['session']['id'], payload['current']['id']
        store.execute('UPDATE dialer_sessions SET auto_advance=0 WHERE id=?', (session_id,))
        with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='CONFIRM')):
            await dialer.start_next(session_id, 100)
        await dialer.process_callback({'cmd': 'history', 'callid': 'CONFIRM', 'status': 'Success'})
        barrier = threading.Barrier(2)
        original = store.rows
        def simultaneous_reads(sql, params=()):
            rows = original(sql, params)
            if 'WHERE id=? AND session_id=?' in sql:
                barrier.wait(5)
            return rows
        async def save(result):
            try:
                await dialer.resolve(session_id, item_id, result, 100)
                return 'saved'
            except dialer.DialerError as error:
                return error.status
        with patch('apps.bot.backend.dialer.store.rows', side_effect=simultaneous_reads), \
             patch('apps.bot.backend.dialer._comment', new=AsyncMock()) as comment:
            results = await asyncio.gather(
                asyncio.to_thread(lambda: asyncio.run(save('answered'))),
                asyncio.to_thread(lambda: asyncio.run(save('failed'))),
            )
        self.assertCountEqual(results, ['saved', 409])
        self.assertEqual(comment.await_count, 1)

    async def test_early_history_is_durable_deduplicated_and_replayed(self):
        payload = await self.create_queue()
        async def provider(*args):
            event = {'cmd': 'history', 'callid': 'EARLY', 'status': 'Success', 'duration': 17, 'auth': 'do-not-store'}
            self.assertTrue(await dialer.process_callback(event))
            self.assertTrue(await dialer.process_callback(event))
            pending = store.rows('SELECT payload FROM dialer_pending_callbacks')
            self.assertEqual(len(pending), 1)
            self.assertNotIn('do-not-store', pending[0]['payload'])
            return 'EARLY'
        with patch('apps.bot.backend.dialer.make_call', side_effect=provider):
            await dialer.start_next(payload['session']['id'], 100)
        # A fresh recovery pass uses only persisted rows, not in-memory state.
        await dialer.replay_pending_callbacks()
        current = dialer.current_session(100)
        self.assertEqual(current['current']['status'], 'awaiting_confirmation')
        self.assertEqual(current['session']['status'], 'paused')
        self.assertEqual(store.rows('SELECT duration_seconds FROM dialer_call_attempts')[0]['duration_seconds'], 17)
        self.assertEqual(store.rows('SELECT * FROM dialer_pending_callbacks'), [])
        self.assertEqual(len(store.rows('SELECT * FROM dialer_call_events')), 1)

    async def test_early_accepted_event_is_replayed(self):
        payload = await self.create_queue()
        async def provider(*args):
            self.assertTrue(await dialer.process_callback({'cmd': 'event', 'type': 'ACCEPTED', 'callid': 'EARLY-CONNECT'}))
            return 'EARLY-CONNECT'
        with patch('apps.bot.backend.dialer.make_call', side_effect=provider):
            await dialer.start_next(payload['session']['id'], 100)
        await dialer.replay_pending_callbacks()
        self.assertEqual(dialer.current_session(100)['current']['status'], 'connected')

    async def test_early_missed_event_advances_only_once(self):
        payload = await self.create_queue()
        sid = payload['session']['id']
        store.execute("INSERT INTO dialer_queue_items(session_id,position,entity_type,entity_id,phone) VALUES(?,2,'lead','99','79990000001')", (sid,))
        async def provider(*args):
            await dialer.process_callback({'cmd': 'history', 'status': 'missed', 'callid': 'EARLY-MISS'})
            return 'EARLY-MISS'
        with patch('apps.bot.backend.dialer.make_call', side_effect=provider):
            await dialer.start_next(sid, 100)
        with patch('apps.bot.backend.dialer.make_call', new=AsyncMock(return_value='NEXT')) as call, \
             patch('apps.bot.backend.dialer._comment', new=AsyncMock()):
            await dialer.replay_pending_callbacks()
            await dialer.replay_pending_callbacks()
        call.assert_awaited_once()
        self.assertEqual(dialer.current_session(100)['current']['last_call_id'], 'NEXT')

    async def test_failed_recovery_keeps_pending_event_for_retry(self):
        payload = await self.create_queue()
        async def provider(*args):
            await dialer.process_callback({'cmd': 'history', 'status': 'Success', 'callid': 'RECOVER'})
            return 'RECOVER'
        with patch('apps.bot.backend.dialer.make_call', side_effect=provider):
            await dialer.start_next(payload['session']['id'], 100)
        with patch('apps.bot.backend.dialer._apply_callback', new=AsyncMock(side_effect=RuntimeError('temporary'))), \
             self.assertLogs('apps.bot.backend.dialer', level='ERROR'):
            await dialer.replay_pending_callbacks()
        self.assertEqual(len(store.rows('SELECT * FROM dialer_pending_callbacks')), 1)
        await dialer.replay_pending_callbacks()
        self.assertEqual(dialer.current_session(100)['current']['status'], 'awaiting_confirmation')

    async def test_unrelated_callbacks_are_ignored_or_expire(self):
        self.assertFalse(await dialer.process_callback({'cmd': 'history', 'callid': 'UNRELATED'}))
        payload = await self.create_queue()
        async def provider(*args):
            await dialer.process_callback({'cmd': 'history', 'callid': 'UNRELATED'})
            return 'ACTUAL'
        with patch('apps.bot.backend.dialer.make_call', side_effect=provider):
            await dialer.start_next(payload['session']['id'], 100)
        store.execute('UPDATE dialer_pending_callbacks SET received_at=0')
        await dialer.replay_pending_callbacks()
        self.assertEqual(store.rows('SELECT * FROM dialer_pending_callbacks'), [])
        self.assertEqual(dialer.current_session(100)['current']['status'], 'calling')

    async def test_session_cannot_be_read_by_another_manager(self):
        payload = await self.create_queue()
        with self.assertRaises(dialer.DialerError) as error:
            dialer.session_payload(payload["session"]["id"], 200)
        self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
