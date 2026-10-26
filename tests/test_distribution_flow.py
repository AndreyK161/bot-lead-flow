import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import distribution_flow, store


class DistributionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(database_path=self.tmp.name + '/db.sqlite', sales_department_id='5')
        self.store_patch = patch('app.store.get_settings', return_value=self.settings)
        self.store_patch.start()
        self.managers = [
            {'bitrix_id': '1', 'name': 'Анна'},
            {'bitrix_id': '2', 'name': 'Борис'},
            {'bitrix_id': '3', 'name': 'Вера'},
            {'bitrix_id': '4', 'name': 'Глеб'},
        ]

    async def asyncTearDown(self):
        self.store_patch.stop()
        self.tmp.cleanup()

    async def test_start_persists_session_and_renders_real_sales_managers(self):
        with patch('app.distribution_flow._sales_managers', new=AsyncMock(return_value=self.managers)), \
             patch('app.distribution_flow.send_telegram_message', new=AsyncMock()) as send:
            await distribution_flow.start(1297686797, 1297686797, self.settings)
        session = store.distribution_session(1297686797)
        self.assertEqual(session['step'], 'managers')
        self.assertEqual(session['payload']['managers'], self.managers)
        buttons = send.await_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual([row[0]['text'] for row in buttons[:-1]], ['⬜ Анна', '⬜ Борис', '⬜ Вера', '⬜ Глеб'])

    async def test_stale_callback_cannot_change_current_session(self):
        store.save_distribution_session(1297686797, 'current', 'managers', {
            'managers': self.managers, 'selected': [], 'selected_stages': [0],
        })
        callback = {
            'id': 'cb', 'from': {'id': 1297686797}, 'data': 'dist:old:manager:0',
            'message': {'chat': {'id': 1297686797}, 'message_id': 10},
        }
        with patch('app.distribution_flow.answer_callback_query', new=AsyncMock()) as answer:
            self.assertTrue(await distribution_flow.handle_callback(callback, self.settings))
        self.assertTrue(answer.await_args.kwargs['show_alert'])
        self.assertEqual(store.distribution_session(1297686797)['payload']['selected'], [])

    async def test_no_more_than_three_managers_can_be_selected(self):
        store.save_distribution_session(1297686797, 'current', 'managers', {
            'managers': self.managers, 'selected': [0, 1, 2], 'selected_stages': [0],
        })
        callback = {
            'id': 'cb', 'from': {'id': 1297686797}, 'data': 'dist:current:manager:3',
            'message': {'chat': {'id': 1297686797}, 'message_id': 10},
        }
        with patch('app.distribution_flow.answer_callback_query', new=AsyncMock()) as answer:
            await distribution_flow.handle_callback(callback, self.settings)
        self.assertIn('не более трёх', answer.await_args.kwargs['text'])
        self.assertEqual(store.distribution_session(1297686797)['payload']['selected'], [0, 1, 2])

    def test_confirmation_is_off_for_unbound_manager_or_multiple_stages(self):
        payload = {'managers': self.managers, 'selected': [0], 'stage_ids': ['IN_PROCESS']}
        self.assertFalse(distribution_flow._can_confirm(payload))
        payload = {
            'managers': [{'bitrix_id': '460', 'name': 'Вячеслав'}],
            'selected': [0], 'stage_ids': ['IN_PROCESS'],
        }
        self.assertTrue(distribution_flow._can_confirm(payload))
        payload['stage_ids'].append('NEW')
        self.assertFalse(distribution_flow._can_confirm(payload))
        payload = {
            'managers': [{'bitrix_id': value, 'name': value} for value in ('50', '110', '460')],
            'selected': [0, 1, 2], 'stage_ids': ['IN_PROCESS'],
        }
        self.assertTrue(distribution_flow._can_confirm(payload))

    def test_confirmation_capability_is_separate_from_distribution_access(self):
        self.settings.mode_switch_user_id = 1297686797
        self.settings.distribution_confirmer_id_set = {8306778724}
        self.assertTrue(distribution_flow._may_confirm(8306778724, self.settings))
        self.assertFalse(distribution_flow._may_confirm(7939114214, self.settings))

    async def test_reminder_is_claimed_once_in_moscow_window(self):
        self.settings.mode_switch_user_id = 1297686797
        store.set_distribution_reminder(1297686797, 1297686797, True)
        now = datetime(2026, 10, 21, 7, 2, tzinfo=timezone.utc)
        with patch('app.distribution_flow.send_telegram_message', new=AsyncMock()) as send:
            self.assertEqual(await distribution_flow.send_due_reminders(self.settings, now=now), 1)
            self.assertEqual(await distribution_flow.send_due_reminders(self.settings, now=now), 0)
        send.assert_awaited_once()
        self.assertEqual(
            send.await_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'],
            'dist:reminder:new',
        )

    async def test_reminder_is_sent_to_enabled_planner_user_after_cutover(self):
        self.settings.mode_switch_user_id = 1297686797
        manager_id = 7939114214
        store.set_distribution_reminder(manager_id, manager_id, True)
        now = datetime(2026, 10, 21, 7, 2, tzinfo=timezone.utc)
        with patch('app.distribution_flow.planner_access.enabled_user_ids', return_value={manager_id}), \
             patch('app.distribution_flow.send_telegram_message', new=AsyncMock()) as send:
            self.assertEqual(await distribution_flow.send_due_reminders(self.settings, now=now), 1)
        self.assertEqual(send.await_args.kwargs['chat_id'], manager_id)


if __name__ == '__main__':
    unittest.main()
