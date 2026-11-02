import tempfile
import unittest
from dataclasses import replace
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

    def test_employee_binding_edits_survive_reinitialization_without_duplicates(self):
        changes = (
            {"bitrix_id": 111},
            {"telegram_id": 900000001},
            {"telegram_id": 900000001, "bitrix_id": 111},
            {"telegram_id": None, "bitrix_id": None},
        )
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "planner.sqlite3"
                store = Store(path)
                store.initialize()
                supervisor = store.actor(1073597301)
                employee = store.actor(7939114214)
                plan = store.create_plan(employee, "2026-10-21")
                store.add_task(employee, plan["id"], {
                    "title": "Existing task", "description": "", "kind": "general",
                    "planned_minutes": 15, "target": None,
                })
                store.save_employee(supervisor, {
                    "name": "Updated employee", "active": False,
                    "telegram_id": employee["telegram_id"],
                    "bitrix_id": employee["bitrix_id"], **change,
                }, employee["id"])
                with store.connection() as connection:
                    before = list(connection.iterdump())

                # A fresh Store models a restart, including repeated startup.
                for _ in range(2):
                    restarted = Store(path)
                    restarted.initialize()
                    with restarted.connection() as connection:
                        self.assertEqual(list(connection.iterdump()), before)
                    saved = next(e for e in restarted.employees(supervisor)
                                 if e["id"] == employee["id"])
                    self.assertEqual(saved["name"], "Updated employee")
                    self.assertEqual(saved["active"], 0)
                    for key, value in change.items():
                        self.assertEqual(saved[key], value)
                    self.assertEqual(restarted.get_plan(supervisor, plan["id"])["tasks"][0]["employee_id"],
                                     employee["id"])

    def test_vyacheslav_telegram_alias_opens_same_manager(self):
        canonical = self.store.actor(8306778724)
        alias = self.store.actor(399005637)
        self.assertEqual(alias['id'], canonical['id'])
        self.assertEqual(alias['bitrix_id'], 460)
        self.assertEqual(alias['role'], 'manager')
        self.assertEqual(alias['name'], 'Вячеслав Шандра')

    def test_another_employees_alias_is_rejected_without_changing_database(self):
        for active in (True, False):
            for editing in (True, False):
                with self.subTest(owner_active=active, editing=editing), tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / "planner.sqlite3")
                    store.initialize()
                    supervisor = store.actor(1073597301)
                    alias_owner = store.actor(8306778724)
                    employee = store.actor(7939114214)
                    if not active:
                        store.save_employee(supervisor, {
                            "name": alias_owner["name"], "telegram_id": alias_owner["telegram_id"],
                            "bitrix_id": alias_owner["bitrix_id"], "active": False,
                        }, alias_owner["id"])
                    with store.connection() as connection:
                        before = list(connection.iterdump())
                    with self.assertRaises(DomainError) as error:
                        store.save_employee(supervisor, {
                            "name": "Conflicting employee", "telegram_id": 399005637,
                            "bitrix_id": employee["bitrix_id"] if editing else 999,
                            "active": False,
                        }, employee["id"] if editing else None)
                    self.assertEqual(error.exception.status, 409)
                    self.assertIn("Telegram ID", error.exception.message)
                    with store.connection() as connection:
                        self.assertEqual(list(connection.iterdump()), before)
                    self.assertEqual(store.actor(employee["telegram_id"])["id"], employee["id"])
                    if active:
                        self.assertEqual(store.actor(399005637)["id"], alias_owner["id"])

    def test_employee_can_use_their_own_alias_as_primary_telegram_id(self):
        supervisor = self.store.actor(1073597301)
        employee = self.store.actor(8306778724)
        saved = self.store.save_employee(supervisor, {
            "name": employee["name"], "telegram_id": 399005637,
            "bitrix_id": employee["bitrix_id"], "active": True,
        }, employee["id"])
        self.assertEqual(saved["telegram_id"], 399005637)
        self.assertEqual(self.store.actor(399005637)["id"], employee["id"])
        self.store.initialize()
        self.assertEqual(self.store.actor(399005637)["id"], employee["id"])

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

    def test_bitrix_deadline_move_creates_new_daily_entry_and_preserves_history(self):
        for close_previous in (False, True):
            with self.subTest(close_previous=close_previous), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "planner.sqlite3")
                store.initialize()
                manager = store.actor(7939114214)
                old_plan = store.create_plan(manager, "2026-10-20")
                new_plan = store.create_plan(manager, "2026-10-21")
                external = ExternalTask(
                    external_id="bitrix:77", title="Call client",
                    url="https://example.invalid/tasks/77/",
                    deadline=datetime.fromisoformat("2026-10-20T12:00:00+03:00"),
                    external_status="2",
                )
                self.assertEqual(store.sync_external_tasks(old_plan["id"], [external]), 1)
                if close_previous:
                    store.close_plan(manager, old_plan["id"])
                with store.connection() as connection:
                    previous_tasks = [dict(row) for row in connection.execute(
                        "SELECT * FROM tasks WHERE plan_id=?", (old_plan["id"],))]
                previous_report = store.report(manager, old_plan["id"]) if close_previous else None
                moved = replace(external, deadline=datetime.fromisoformat("2026-10-21T12:00:00+03:00"))

                self.assertEqual(store.sync_external_tasks(new_plan["id"], [moved]), 1)
                self.assertEqual(store.sync_external_tasks(new_plan["id"], [moved]), 0)
                updated = replace(moved, title="Updated in Bitrix", external_status="5")
                self.assertEqual(store.sync_external_tasks(new_plan["id"], [updated]), 1)
                self.assertEqual(store.sync_external_tasks(new_plan["id"], [updated]), 0)
                tasks = store.get_plan(manager, new_plan["id"])["tasks"]
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["title"], updated.title)
                self.assertEqual(tasks[0]["status"], "done")
                self.assertIn("2026-10-21", tasks[0]["description"])
                with store.connection() as connection:
                    self.assertEqual([dict(row) for row in connection.execute(
                        "SELECT * FROM tasks WHERE plan_id=?", (old_plan["id"],))], previous_tasks)
                if close_previous:
                    self.assertEqual(store.sync_external_tasks(old_plan["id"], [updated]), 0)
                    self.assertEqual(store.report(manager, old_plan["id"]), previous_report)

    def test_reassigned_bitrix_task_can_appear_in_new_employees_plan(self):
        first = self.store.actor(7939114214)
        second = self.store.actor(7874883320)
        first_plan = self.store.create_plan(first, "2026-10-21")
        second_plan = self.store.create_plan(second, "2026-10-21")
        external = ExternalTask(
            external_id="bitrix:77", title="Call client", url="https://example.invalid/tasks/77/",
            deadline=datetime.fromisoformat("2026-10-21T12:00:00+03:00"), external_status="2",
        )
        self.assertEqual(self.store.sync_external_tasks(first_plan["id"], [external]), 1)
        self.assertEqual(self.store.sync_external_tasks(second_plan["id"], [external]), 1)
        self.assertEqual(self.store.sync_external_tasks(second_plan["id"], [external]), 0)
        tasks = self.store.get_plan(second, second_plan["id"])["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["employee_id"], second["id"])
        with self.assertRaises(DomainError):
            self.store.get_plan(first, second_plan["id"])

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
