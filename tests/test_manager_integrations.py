import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import store
from app.manager_planner.integrations import PlannerCRM


class PlannerCRMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + '/lead-flow.sqlite3',
            database_url='',
            daily_stats_timezone='Europe/Moscow',
            megafon_bitrix_users={'101': '110', '102': '50'},
            bitrix_webhook_url='https://portal.example/rest/24/token/',
        )
        self.settings_patch = patch('app.manager_planner.integrations.get_settings', return_value=self.settings)
        self.store_patch = patch('app.store.get_settings', return_value=self.settings)
        self.settings_patch.start(); self.store_patch.start()
        self.client = AsyncMock()
        self.client.get_lead_statuses.return_value = []
        self.crm = PlannerCRM(self.client)

    async def asyncTearDown(self):
        self.settings_patch.stop(); self.store_patch.stop()
        self.tmp.cleanup()

    async def test_tasks_are_scoped_to_responsible_employee_and_deadline_day(self):
        self.client.get_tasks_by_deadline.return_value = [
            {'id': '77', 'title': 'Позвонить', 'responsibleId': '110', 'status': '2',
             'deadline': '2026-10-21T12:00:00+03:00', 'closedDate': None},
            {'id': '88', 'title': 'Чужая', 'responsibleId': '50', 'status': '2',
             'deadline': '2026-10-21T13:00:00+03:00', 'closedDate': None},
        ]
        tasks = await self.crm.tasks(110, '2026-10-21')
        self.assertEqual([task.external_id for task in tasks], ['bitrix:77'])
        self.assertIn('/user/110/tasks/task/view/77/', tasks[0].url)
        args = self.client.get_tasks_by_deadline.await_args.args
        self.assertIn('2026-10-21T00:00:00+03:00', args[0])
        self.assertEqual(args[2], ['110'])

    async def test_call_statistics_use_megafon_binding_and_duration(self):
        for call_id, duration, status, direction in (
            ('a', 20, 'Success', 'out'), ('b', 240, 'Success', 'out'),
            ('c', 0, 'Failed', 'out'), ('d', 80, 'Success', 'in'),
        ):
            store.execute(
                """INSERT INTO megafon_calls(
                   call_id,manager_extension,direction,client_phone,started_at,
                   duration_seconds,call_status) VALUES(?,?,?,?,?,?,?)""",
                (call_id, '101', direction, '9990000000',
                 datetime.fromisoformat('2026-10-21T09:00:00+00:00').isoformat(), duration, status),
            )
        result = await self.crm.call_statistics(110, '2026-10-21')
        self.assertTrue(result['configured'])
        self.assertEqual(result['total'], 4)
        self.assertEqual(result['outbound'], 3)
        self.assertEqual(result['answered'], 2)
        self.assertEqual(result['long_calls'], 1)
        self.assertEqual(result['duration_seconds'], 260)

    async def test_summary_contains_only_the_two_configured_pipelines(self):
        self.client.get_user_deals.side_effect = [
            [{'STAGE_ID': 'NEW', 'OPPORTUNITY': '1000'}],
            [{'STAGE_ID': 'C2:NEW', 'OPPORTUNITY': '2500'}],
        ]
        result = await self.crm.summary(110, '2026-10-21')
        self.assertTrue(result['available'])
        self.assertEqual([item['category_id'] for item in result['pipelines']], [0, 2])
        self.assertEqual([item['amount'] for item in result['pipelines']], [1000.0, 2500.0])

    async def test_processed_leads_are_scoped_by_manager_and_moscow_day(self):
        self.client.get_lead_statuses.return_value = [
            {"STATUS_ID": "IN_PROCESS", "NAME": "В работе"},
            {"STATUS_ID": "JUNK", "NAME": "Мусор"},
        ]
        store.execute(
            """INSERT INTO crm_items(
               entity_type,entity_id,created_date,current_stage_id,current_stage_name,
               current_assignee_id,processed_at,processed_by_id,first_seen_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ('lead', '1', '2026-10-21', 'IN_PROCESS', 'В работе', '110',
             '2026-10-21T12:00:00+03:00', '110', '2026-10-21T09:00:00+03:00',
             '2026-10-21T12:00:00+03:00'),
        )
        store.execute(
            """INSERT INTO crm_items(
               entity_type,entity_id,created_date,current_stage_id,current_stage_name,
               current_assignee_id,processed_at,processed_by_id,first_seen_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ('lead', '2', '2026-10-21', 'JUNK', 'Мусор', '50',
             '2026-10-21T13:00:00+03:00', '50', '2026-10-21T10:00:00+03:00',
             '2026-10-21T13:00:00+03:00'),
        )
        result = await self.crm.processed_leads(110, '2026-10-21')
        self.assertEqual(result, {'processed': 1, 'stages': {'В работе': 1}})

    async def test_processed_leads_replace_stored_stage_id_with_bitrix_name(self):
        self.client.get_lead_statuses.return_value = [
            {"STATUS_ID": "UC_9HGM2H", "NAME": "Недозвон"},
        ]
        store.execute(
            """INSERT INTO crm_items(
               entity_type,entity_id,created_date,current_stage_id,current_stage_name,
               current_assignee_id,processed_at,processed_by_id,first_seen_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ('lead', '3', '2026-10-21', 'UC_9HGM2H', 'UC_9HGM2H', '110',
             '2026-10-21T12:00:00+03:00', '110', '2026-10-21T09:00:00+03:00',
             '2026-10-21T12:00:00+03:00'),
        )
        result = await self.crm.processed_leads(110, '2026-10-21')
        self.assertEqual(result['stages'], {'Недозвон': 1})


if __name__ == '__main__':
    unittest.main()
