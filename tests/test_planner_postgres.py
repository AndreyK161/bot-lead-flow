"""Explicit opt-in integration tests; only a disposable planner_test DB is allowed.

PLANNER_TEST_DATABASE_URL must point at a local test instance, not production.
The suite recreates ONLY the planner namespace in that disposable database.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from apps.planner.backend.store import DomainError, Store, configured_store
from apps.planner.backend.distribution_import import import_snapshot
from apps.planner.backend.distribution_queue import DistributionQueue
from apps.planner.backend.sync import sync_once

TEST_URL = os.environ.get('PLANNER_TEST_DATABASE_URL', '')


class StorageSelectionTests(unittest.TestCase):
    def test_core_database_url_does_not_switch_planner_implicitly(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://unused/core'}):
            selected = configured_store(Path('unused.sqlite3'))
            self.assertIs(type(selected), Store)
            self.assertEqual(selected.database_url, '')


@unittest.skipUnless(TEST_URL, 'Set PLANNER_TEST_DATABASE_URL for disposable PostgreSQL integration tests')
class PlannerPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from apps.planner.backend.postgres import PostgresStore
        info = conninfo_to_dict(TEST_URL)
        if info.get('dbname') != 'planner_test' or info.get('host') not in ('planner-postgres-test', 'localhost', '127.0.0.1'):
            raise RuntimeError('Integration tests require a disposable planner_test database on a test host')
        cls.psycopg = psycopg
        cls.store_class = PostgresStore
        with psycopg.connect(TEST_URL) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS public.core_sentinel(id bigint PRIMARY KEY, value text NOT NULL)')
            conn.execute("INSERT INTO public.core_sentinel VALUES(1,'bot data') ON CONFLICT(id) DO NOTHING")

    def setUp(self):
        with self.psycopg.connect(TEST_URL) as conn:
            conn.execute('DROP SCHEMA IF EXISTS planner CASCADE')
        self.store = self.store_class(TEST_URL)
        self.store.initialize()
        self.manager = self.store.actor(7939114214)
        self.supervisor = self.store.actor(1073597301)
        self.clock = patch('apps.planner.backend.store.today', return_value='2026-11-02')
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def tearDown(self):
        with self.psycopg.connect(TEST_URL) as conn:
            self.assertEqual(conn.execute('SELECT value FROM public.core_sentinel WHERE id=1').fetchone()[0], 'bot data')

    def task(self, plan=None, title='Задача ? 100%'):
        if plan is None:
            plan = self.store.create_plan(self.manager, '2026-11-02')
        return self.store.add_task(self.manager, plan['id'], {
            'title': title, 'description': 'Кириллица 🙂', 'kind': 'quantitative',
            'planned_minutes': 15, 'target': 10,
        })

    def snapshot(self):
        return {
            'schema': 1, 'distribution_id': str(uuid4()), 'workday': '2026-11-02',
            'confirmed_at': '2026-11-02T10:00:00+03:00', 'confirmed_by': 1297686797,
            'variant_index': 0, 'variant_id': 'test', 'stage_id': 'IN_PROCESS', 'total': 1,
            'allocations': [{'telegram_id': 7939114214, 'bitrix_id': 110, 'name': 'Яна',
                             'dates': ['2026-11-01'], 'count': 1,
                             'leads': [{'id': 2147483648, 'created_date': '2026-11-01', 'stage_id': 'IN_PROCESS'}]}],
        }

    def test_bootstrap_big_ids_aliases_and_repeated_initialization(self):
        self.assertEqual(self.store.actor(399005637), self.store.actor(8306778724))
        self.assertEqual(self.manager['telegram_id'], 7939114214)
        saved = self.store.save_employee(self.supervisor, {
            'name': 'AndreyK', 'telegram_id': 699296552, 'bitrix_id': None, 'active': True,
        })
        self.store.initialize()
        self.assertEqual(self.store.actor(699296552)['id'], saved['id'])
        with self.store.connection() as db:
            self.assertEqual(self.store.schema_version(db), 8)

    def test_sqlite_and_postgres_have_the_same_transfer_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            sqlite = Store(Path(directory) / 'source.sqlite3')
            sqlite.initialize()
            with sqlite.connection() as source, self.store.connection() as target:
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                pg_tables = {r['table_name'] for r in target.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='planner'"
                )}
                self.assertEqual(tables, pg_tables - {'storage_metadata'})
                for table in sorted(tables):
                    columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
                    pg_columns = [r['column_name'] for r in target.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='planner' AND table_name=? ORDER BY ordinal_position", (table,)
                    )]
                    self.assertEqual(columns, pg_columns, table)

    def test_full_plan_goal_timer_report_workflow(self):
        plan = self.store.create_plan(self.manager, '2026-11-02')
        task = self.task(plan)
        self.assertEqual(task['title'], 'Задача ? 100%')
        self.store.confirm_plan(self.manager, plan['id'])
        request = self.store.request_goal(self.manager, task['id'], {
            'field': 'planned_minutes', 'proposed': 25, 'reason': 'Нужно больше времени',
        })
        self.store.decide(self.supervisor, request['id'], 'approved', 'Согласовано')
        changed = self.store.timer(self.manager, task['id'], 'start')
        self.assertIsNotNone(changed['timer_started'])
        self.store.timer(self.manager, task['id'], 'stop')
        report = self.store.close_plan(self.manager, plan['id'])
        self.assertEqual(report, self.store.report(self.supervisor, plan['id']))
        self.assertEqual(report['tasks'][0]['planned_minutes'], 25)
        self.assertTrue(self.store.history(self.manager, plan['id']))
        self.assertEqual(len(self.store.list_plans(self.supervisor)), 1)
        with self.assertRaises(DomainError):
            self.task(plan)

    def test_prior_workday_copy_skips_weekend(self):
        friday = self.store.create_plan(self.manager, '2026-10-30')
        self.task(friday, 'Пятница')
        saturday = self.store.create_plan(self.manager, '2026-10-31')
        self.task(saturday, 'Суббота')
        monday = self.store.create_plan(self.manager, '2026-11-02', from_previous=True)
        self.assertEqual([t['title'] for t in monday['tasks']], ['Пятница'])

    def test_move_keeps_references_and_cancels_pending_request(self):
        task = self.task()
        self.store.request_goal(self.manager, task['id'], {'field': 'target', 'proposed': 20, 'reason': 'Изменение'})
        moved = self.store.move(self.manager, task['id'], '2026-11-03', 'Завтра', task['revision'])
        self.assertEqual(moved['source_task_id'], task['id'])
        self.assertEqual(self.store.requests(self.manager)[0]['status'], 'cancelled')

    def test_write_rollback_including_audit(self):
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.store.connection(True) as db:
                db.execute("UPDATE employees SET name='lost' WHERE id=?", (self.manager['id'],))
                self.store.audit(db, self.manager, 'employee', self.manager['id'], 'test', None, {})
                raise RuntimeError('rollback')
        self.assertEqual(self.store.actor(self.manager['telegram_id'])['name'], self.manager['name'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM audit').fetchone()['total'], 0)

    def test_unique_and_foreign_key_constraints(self):
        with self.assertRaises(self.psycopg.IntegrityError):
            self.store.save_employee(self.supervisor, {'name': 'Duplicate', 'telegram_id': self.manager['telegram_id'], 'bitrix_id': None, 'active': True})
        with self.assertRaises(self.psycopg.IntegrityError):
            with self.store.connection(True) as db:
                db.execute('INSERT INTO employee_aliases VALUES(?,?)', (123, 999999))
        self.assertEqual(self.store.actor(self.manager['telegram_id'])['name'], self.manager['name'])

    def test_read_transactions_cannot_write(self):
        with self.assertRaises(self.psycopg.errors.ReadOnlySqlTransaction):
            with self.store.connection() as db:
                db.execute("UPDATE employees SET name='invalid'")

    def test_no_fallback_to_public_schema(self):
        with self.psycopg.connect(TEST_URL) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS public.planner_canary(id bigint)')
        with self.assertRaises(self.psycopg.errors.UndefinedTable):
            with self.store.connection() as db:
                db.execute('SELECT * FROM planner_canary')

    def test_immutable_audit_reports_and_system_audit(self):
        task = self.task()
        self.store.close_plan(self.manager, task['plan_id'])
        with self.store.connection(True) as db:
            db.execute("INSERT INTO system_audit(plan_id,entity,entity_id,action,after_json,created_at) VALUES(?,'task',?,'test','{}',1)", (task['plan_id'], task['id']))
        for table, column in [('reports', 'snapshot'), ('audit', 'action'), ('system_audit', 'action')]:
            for statement in (f"UPDATE {table} SET {column}='changed'", f'DELETE FROM {table}', f'TRUNCATE {table}'):
                with self.subTest(statement=statement), self.assertRaises(self.psycopg.IntegrityError):
                    with self.store.connection(True) as db:
                        db.execute(statement)
            with self.store.connection() as db:
                self.assertGreater(db.execute(f'SELECT count(*) AS total FROM {table}').fetchone()['total'], 0)

    def test_parallel_revision_changes_only_one_wins(self):
        task = self.task()
        def change(title):
            try:
                return self.store.patch_task(self.manager, task['id'], {'revision': task['revision'], 'title': title})['title']
            except DomainError:
                return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(change, ['Первый', 'Второй']))
        self.assertEqual(results.count('conflict'), 1)

    def test_parallel_timers_only_one_starts(self):
        plan = self.store.create_plan(self.manager, '2026-11-02')
        tasks = [self.task(plan, name) for name in ('Первый', 'Второй')]
        def start(task):
            try:
                self.store.timer(self.manager, task['id'], 'start')
                return 'started'
            except DomainError:
                return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(start, tasks))
        self.assertEqual(sorted(results), ['conflict', 'started'])

    def test_parallel_distribution_import_is_idempotent(self):
        snapshot = self.snapshot()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: import_snapshot(self.store, snapshot, {1297686797}), range(2)))
        self.assertEqual([r['status'] for r in results], ['imported', 'imported'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM tasks').fetchone()['total'], 1)
            self.assertEqual(db.execute('SELECT count(*) AS total FROM system_audit').fetchone()['total'], 1)
        plan = self.store.list_plans(self.manager)[0]
        task = self.store.get_plan(self.manager, plan['id'])['tasks'][0]
        self.assertTrue(task['distribution_target_locked'])
        self.assertEqual(task['distribution_source']['count'], 1)

    def test_legacy_queue_repeated_import_records_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = DistributionQueue(Path(directory) / 'queue.sqlite3')
            queue.initialize()
            queue.publish(self.snapshot(), {1297686797})
            self.assertEqual(sync_once(self.store, queue.path, {1297686797}), ['imported'])
            self.assertEqual(sync_once(self.store, queue.path, {1297686797}), [])


    def test_read_snapshot_is_consistent_while_another_transaction_writes(self):
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM plans').fetchone()['total'], 0)
            self.store.create_plan(self.manager, '2026-11-02')
            self.assertEqual(db.execute('SELECT count(*) AS total FROM plans').fetchone()['total'], 0)
        self.assertEqual(len(self.store.list_plans(self.manager)), 1)

    def test_explicit_selection_and_api_initialization(self):
        from types import SimpleNamespace
        from apps.planner.backend import api
        settings = SimpleNamespace(manager_planner_database_path='/nonexistent/never-create.sqlite3',
                                   manager_planner_database_url=TEST_URL)
        with patch.object(api, 'store', Store(Path(settings.manager_planner_database_path))), patch.object(api, 'get_settings', return_value=settings):
            api.initialize()
            self.assertIsInstance(api.store, self.store_class)
            self.assertEqual(api.store.actor(self.manager['telegram_id']), self.manager)
            selected = api.store
            api.initialize()
            self.assertIs(api.store, selected)

    def test_api_maps_postgres_errors_without_exposing_details(self):
        from fastapi.testclient import TestClient
        from apps.planner.backend import api
        with patch.object(api, 'store', self.store):
            app = api.create_app()
            @app.get('/_test/conflict')
            def conflict():
                raise self.psycopg.errors.UniqueViolation('secret connection details')
            app.router.routes.insert(0, app.router.routes.pop())
            @app.get('/_test/unavailable')
            def unavailable():
                raise self.psycopg.OperationalError('secret connection details')
            app.router.routes.insert(0, app.router.routes.pop())
            with TestClient(app) as client:
                self.assertEqual(client.get('/healthz').status_code, 200)
                for path, status in [('/_test/conflict', 409), ('/_test/unavailable', 503)]:
                    response = client.get(path)
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn('secret', response.text)

    def test_initialization_rejects_unknown_or_future_schema(self):
        with self.store.connection(True) as db:
            db.execute('UPDATE storage_metadata SET version=999')
        with self.assertRaises(RuntimeError):
            self.store.initialize()
        with self.psycopg.connect(TEST_URL) as conn:
            conn.execute('DROP SCHEMA planner CASCADE')
            conn.execute('CREATE SCHEMA planner')
            conn.execute('CREATE TABLE planner.unknown(id bigint)')
        with self.assertRaises(RuntimeError):
            self.store.initialize()
