import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.manager_planner.distribution_import import import_snapshot
from app.manager_planner.distribution_queue import DistributionQueue
from app.manager_planner.sync import sync_once
from app.manager_planner.store import DomainError, Store
from app.manager_planner.integrations import ExternalTask


class IsolatedManagerPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'manager.sqlite3')
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_seeds_owner_and_legacy_manager_bindings(self):
        owner = self.store.actor(1297686797)
        self.assertEqual(owner['role'], 'manager')
        self.assertEqual(owner['name'], 'Тестовый менеджер')
        with self.store.connection() as connection:
            bindings = connection.execute(
                'SELECT telegram_id,bitrix_id FROM employees WHERE bitrix_id IS NOT NULL ORDER BY bitrix_id'
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in bindings],
            [(7874883320, 50), (7939114214, 110), (8306778724, 460)],
        )
        supervisor = self.store.actor(1073597301)
        self.assertEqual(supervisor['role'], 'supervisor')

    def test_vyacheslav_telegram_alias_opens_same_manager(self):
        canonical = self.store.actor(8306778724)
        alias = self.store.actor(399005637)
        self.assertEqual(alias['id'], canonical['id'])
        self.assertEqual(alias['bitrix_id'], 460)
        self.assertEqual(alias['role'], 'manager')
        self.assertEqual(alias['name'], 'Вячеслав Шандра')

    def test_owner_can_create_an_independent_daily_plan(self):
        owner = self.store.actor(1297686797)
        plan = self.store.create_plan(owner, '2026-10-21', None, False)
        self.assertEqual(plan['employee_id'], owner['id'])
        self.assertEqual(plan['status'], 'draft')

    def test_manager_cannot_read_another_employee_crm_context(self):
        yana = self.store.actor(7939114214)
        evgeny = self.store.actor(7874883320)
        with self.assertRaises(DomainError):
            self.store.employee(yana, evgeny['id'])
        supervisor = self.store.actor(1073597301)
        self.assertEqual(self.store.employee(supervisor, evgeny['id'])['bitrix_id'], 50)

    def test_owner_confirmation_imports_tasks_only_into_isolated_database(self):
        value = {
            'schema': 1, 'distribution_id': str(uuid4()), 'workday': '2026-10-21',
            'confirmed_at': '2026-10-21T10:00:00+03:00', 'confirmed_by': 1297686797,
            'variant_index': 0, 'variant_id': 'test', 'stage_id': 'IN_PROCESS', 'total': 2,
            'allocations': [
                {'telegram_id': 8306778724, 'bitrix_id': 460, 'name': 'Вячеслав',
                 'dates': ['2026-10-20'], 'count': 1,
                 'leads': [{'id': 10, 'created_date': '2026-10-20', 'stage_id': 'IN_PROCESS'}]},
                {'telegram_id': 7939114214, 'bitrix_id': 110, 'name': 'Яна',
                 'dates': ['2026-10-19'], 'count': 1,
                 'leads': [{'id': 20, 'created_date': '2026-10-19', 'stage_id': 'IN_PROCESS'}]},
            ],
        }
        result = import_snapshot(self.store, value, {1297686797})
        self.assertEqual(result['status'], 'imported')
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(1) FROM tasks').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT count(1) FROM distribution_leads').fetchone()[0], 2)

    def test_distribution_can_be_mirrored_to_owner_plan_idempotently(self):
        first = self.store.mirror_distribution_for_tester(
            1297686797, 'distribution-1', 7, ['2026-10-19', '2026-10-20'],
        )
        second = self.store.mirror_distribution_for_tester(
            1297686797, 'distribution-1', 7, ['2026-10-19', '2026-10-20'],
        )
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['target'], 7)
        self.assertEqual(first['creation_actor'], 'system')
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(1) FROM tasks WHERE external_id='migration-test:distribution-1'"
            ).fetchone()[0], 1)

    def test_legacy_queue_bridge_imports_new_confirmation_once(self):
        queue_path = Path(self.tmp.name) / 'legacy-confirmations.db'
        queue = DistributionQueue(queue_path)
        queue.initialize()
        value = {
            'schema': 1, 'distribution_id': str(uuid4()), 'workday': '2026-10-21',
            'confirmed_at': '2026-10-21T10:00:00+03:00', 'confirmed_by': 8306778724,
            'variant_index': 0, 'variant_id': 'legacy-bridge', 'stage_id': 'IN_PROCESS', 'total': 1,
            'allocations': [{
                'telegram_id': 8306778724, 'bitrix_id': 460, 'name': 'Вячеслав',
                'dates': ['2026-10-20'], 'count': 1,
                'leads': [{'id': 10, 'created_date': '2026-10-20', 'stage_id': 'IN_PROCESS'}],
            }],
        }
        self.assertTrue(queue.publish(value, {8306778724}))
        self.assertEqual(sync_once(self.store, queue_path, {8306778724}), ['imported'])
        self.assertEqual(sync_once(self.store, queue_path, {8306778724}), [])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM distributions').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM distribution_tasks').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
            attempt = connection.execute(
                'SELECT status,error_code FROM import_attempts WHERE id=?',
                (value['distribution_id'],),
            ).fetchone()
        self.assertEqual(tuple(attempt), ('imported', ''))

    def test_external_bitrix_tasks_are_read_only_and_sync_idempotently(self):
        yana = self.store.actor(7939114214)
        plan = self.store.create_plan(yana, '2026-10-21', None, False)
        external = ExternalTask(
            external_id='bitrix:77', title='Позвонить клиенту',
            url='https://example.bitrix24.ru/company/personal/user/110/tasks/task/view/77/',
            deadline=datetime.fromisoformat('2026-10-21T12:00:00+03:00'),
            external_status='2',
        )
        self.assertEqual(self.store.sync_external_tasks(plan['id'], [external]), 1)
        self.assertEqual(self.store.sync_external_tasks(plan['id'], [external]), 0)
        detail = self.store.get_plan(yana, plan['id'])
        task = detail['tasks'][0]
        self.assertEqual(task['kind'], 'bitrix')
        self.assertEqual(task['external_id'], 'bitrix:77')
        with self.assertRaises(DomainError):
            self.store.patch_task(yana, task['id'], {'revision': task['revision'], 'status': 'done'})


if __name__ == '__main__':
    unittest.main()
