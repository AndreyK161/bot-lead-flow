"""Migration regressions use synthetic snapshots, never local application data."""
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from apps.planner.backend import sqlite_transfer as migration
from apps.planner.backend.distribution_import import import_snapshot
from apps.planner.backend.store import DomainError, Store
from scripts.migrate_planner_to_postgres import main

TEST_URL = os.environ.get('PLANNER_TEST_DATABASE_URL', '')


def fixture(path):
    store = Store(path)
    store.initialize()
    manager = store.actor(7939114214)
    supervisor = store.actor(1073597301)
    values = {'title': 'Текст ? 100% 🙂\nстрока', 'description': 'Кавычки " и \\',
              'kind': 'quantitative', 'planned_minutes': 15, 'target': 2.75}
    with patch('apps.planner.backend.store.today', return_value='2026-11-02'):
        plan = store.create_plan(manager, '2026-10-30')
        task = store.add_task(manager, plan['id'], values)
        store.confirm_plan(manager, plan['id'])
        request = store.request_goal(manager, task['id'], {'field': 'target', 'proposed': 10, 'reason': 'Причина'})
        store.decide(supervisor, request['id'], 'approved', 'Да')
        current = store.get_plan(manager, plan['id'])['tasks'][0]
        moved = store.move(manager, task['id'], '2026-11-03', 'Перенос', current['revision'])
        store.request_goal(manager, moved['id'], {'field': 'planned_minutes', 'proposed': 25, 'reason': 'Время'})
        store.close_plan(manager, plan['id'])
        active = store.create_plan(manager, '2026-11-02')
        timed = store.add_task(manager, active['id'], values)
        store.timer(manager, timed['id'], 'start')
    value = {
        'schema': 1, 'distribution_id': str(uuid4()), 'workday': '2026-11-04',
        'confirmed_at': '2026-11-04T10:00:00+03:00', 'confirmed_by': 1297686797,
        'variant_index': 0, 'variant_id': 'test', 'stage_id': 'IN_PROCESS', 'total': 1,
        'allocations': [{'telegram_id': 7939114214, 'bitrix_id': 110, 'name': 'Яна',
                        'dates': ['2026-11-01'], 'count': 1,
                        'leads': [{'id': 2147483648, 'created_date': '2026-11-01', 'stage_id': 'IN_PROCESS'}]}],
    }
    import_snapshot(store, value, {1297686797})
    with store.connection(True) as db:
        db.execute("INSERT INTO employees(id,name,telegram_id,role) VALUES(1001,'AndreyK',699296552,'supervisor')")
        for identifier in ('a', 'A', 'é', 'Ё'):
            db.execute("INSERT INTO import_attempts VALUES(?,'error',3,1,'retry')", (identifier,))
        db.execute('INSERT INTO legacy_imports VALUES(?,?,?)', ('f' * 64, 1234, '{"импорт":"🙂"}'))
    return store, manager, plan


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PlannerSourceInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'snapshot.sqlite3'
        self.store, self.manager, self.plan = fixture(self.path)

    def test_dry_run_is_repeatable_and_does_not_change_source(self):
        before = file_hash(self.path)
        self.path.chmod(0o400)
        report = migration.inspect_source(self.path)
        self.assertEqual(report, migration.inspect_source(self.path))
        self.assertEqual(set(report['tables']), set(migration.TABLES))
        self.assertTrue(all(table['rows'] > 0 for table in report['tables'].values()))
        self.assertFalse(report['target_checked'])
        self.assertEqual(file_hash(self.path), before)

    def test_missing_source_is_not_created(self):
        missing = self.path.with_name('missing.sqlite3')
        with self.assertRaises(FileNotFoundError):
            migration.inspect_source(missing)
        self.assertFalse(missing.exists())

    def test_wrong_version_unknown_tables_and_changed_columns_are_rejected(self):
        for statement in ('PRAGMA user_version=7', 'CREATE TABLE unexpected(id INTEGER)',
                          'ALTER TABLE employees ADD COLUMN unexpected TEXT'):
            with self.subTest(statement=statement):
                other = Path(self.tmp.name) / (uuid4().hex + '.sqlite3')
                self.path.replace(other)
                # Each case operates on an independent synthetic copy.
                self.path.write_bytes(other.read_bytes())
                with sqlite3.connect(other) as db:
                    db.execute(statement)
                before = file_hash(other)
                with self.assertRaises(migration.TransferError):
                    migration.inspect_source(other)
                self.assertEqual(file_hash(other), before)

    def test_broken_foreign_key_is_rejected_without_repairing_source(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE tasks SET employee_id=999999')
        before = file_hash(self.path)
        with self.assertRaisesRegex(migration.TransferError, 'foreign key'):
            migration.inspect_source(self.path)
        self.assertEqual(file_hash(self.path), before)

    def test_unrepresentable_values_are_rejected(self):
        for column, value in [('title', 'NUL\x00text'), ('title', b'blob'), ('target', float('inf'))]:
            with self.subTest(column=column, kind=type(value).__name__):
                with self.store.connection(True) as db:
                    db.execute(f'UPDATE tasks SET {column}=? WHERE id=1', (value,))
                with self.assertRaises(migration.TransferError):
                    migration.inspect_source(self.path)
                with self.store.connection(True) as db:
                    db.execute("UPDATE tasks SET title='restored',target=2.75 WHERE id=1")

    def test_equal_counts_do_not_hide_changed_content(self):
        old = migration.inspect_source(self.path)
        with self.store.connection(True) as db:
            db.execute("UPDATE employees SET name='Другой текст' WHERE id=1")
        new = migration.inspect_source(self.path)
        self.assertEqual(old['tables']['employees']['rows'], new['tables']['employees']['rows'])
        self.assertNotEqual(old['source_data_sha256'], new['source_data_sha256'])

    def test_cli_default_never_connects_to_postgres_even_when_env_is_set(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'PLANNER_MIGRATION_DATABASE_URL': 'invalid-secret-url'}), \
                patch('scripts.migrate_planner_to_postgres.inspect_target', side_effect=AssertionError('must not connect')), \
                redirect_stdout(output):
            self.assertEqual(main(['--source', str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())['mode'], 'dry-run')
        self.assertNotIn('invalid-secret-url', output.getvalue())

    def test_cli_does_not_print_driver_exception_details(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'PLANNER_MIGRATION_DATABASE_URL': 'private-connection-url'}), \
                patch('scripts.migrate_planner_to_postgres.transfer', side_effect=RuntimeError('secret password and row data')), \
                redirect_stderr(output):
            self.assertEqual(main(['--source', str(self.path), '--apply', '--expected-source-sha256', 'a' * 64]), 1)
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('private-connection-url', output.getvalue())

    def test_cli_requires_reviewed_digest_and_a_separate_destination_setting(self):
        with patch.dict(os.environ, {'PLANNER_MIGRATION_DATABASE_URL': '', 'DATABASE_URL': 'unused'}), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--source', str(self.path), '--check-target'])
        with patch.dict(os.environ, {'PLANNER_MIGRATION_DATABASE_URL': 'unused'}), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--source', str(self.path), '--apply'])


@unittest.skipUnless(TEST_URL, 'Set PLANNER_TEST_DATABASE_URL for disposable PostgreSQL migration tests')
class PlannerPostgresTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from apps.planner.backend.postgres import PostgresStore
        info = conninfo_to_dict(TEST_URL)
        if info.get('dbname') != 'planner_test' or info.get('host') not in ('planner-postgres-test', 'localhost', '127.0.0.1'):
            raise RuntimeError('Only a disposable planner_test database on a test host is allowed')
        cls.psycopg, cls.pg_store = psycopg, PostgresStore
        with psycopg.connect(TEST_URL) as db:
            db.execute('CREATE TABLE IF NOT EXISTS public.core_sentinel(id bigint PRIMARY KEY,value text NOT NULL)')
            db.execute("INSERT INTO public.core_sentinel VALUES(1,'bot data') ON CONFLICT DO NOTHING")

    def setUp(self):
        with self.psycopg.connect(TEST_URL) as db:
            db.execute('DROP SCHEMA IF EXISTS planner CASCADE')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'snapshot.sqlite3'
        self.source, self.manager, self.plan = fixture(self.path)
        self.before = file_hash(self.path)
        self.review = migration.inspect_source(self.path)

    def tearDown(self):
        self.assertEqual(file_hash(self.path), self.before)
        with self.psycopg.connect(TEST_URL) as db:
            self.assertEqual(db.execute('SELECT value FROM public.core_sentinel WHERE id=1').fetchone()[0], 'bot data')

    def apply(self):
        return migration.transfer(self.path, TEST_URL, expected_source_sha256=self.review['source_data_sha256'])

    def assert_no_namespace(self):
        with self.psycopg.connect(TEST_URL) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM pg_namespace WHERE nspname='planner'").fetchone())

    def test_full_transfer_preserves_data_bindings_reports_and_new_ids(self):
        result = self.apply()
        self.assertTrue(result['verified'])
        self.assertEqual(result['tables'], self.review['tables'])
        target = self.pg_store(TEST_URL)
        target.initialize()
        self.assertEqual(target.actor(699296552)['role'], 'supervisor')
        self.assertEqual(target.actor(699296552)['id'], 1001)
        self.assertEqual(target.actor(399005637), self.source.actor(399005637))
        self.assertEqual(target.report(self.manager, self.plan['id']), self.source.report(self.manager, self.plan['id']))
        with target.connection() as pg, self.source.connection() as source:
            for table in migration.IDENTITY_TABLES:
                maximum = source.execute(f'SELECT max(id) FROM "{table}"').fetchone()[0]
                # Sequence state is inspected without consuming any IDs.
                sequence = pg.execute('SELECT pg_get_serial_sequence(?,?) AS name', ('planner.' + table, 'id')).fetchone()['name']
                row = pg.connection.execute('SELECT last_value,is_called FROM ' + sequence).fetchone()
                self.assertEqual(row['last_value'], max(1, maximum + 1), table)
                self.assertFalse(row['is_called'], table)
        supervisor = target.actor(699296552)
        created = target.save_employee(supervisor, {'name': 'Новый', 'telegram_id': 8888888888, 'bitrix_id': None, 'active': True})
        self.assertEqual(created['id'], 1002)
        with self.assertRaises(self.psycopg.IntegrityError):
            with target.connection(True) as db:
                db.execute('DELETE FROM reports')

    def test_post_transfer_verification_is_read_only_and_detects_changes(self):
        self.apply()
        report = migration.verify_transfer(self.path, TEST_URL)
        self.assertEqual(report['mode'], 'verified')
        self.assertEqual(report['tables'], self.review['tables'])
        with self.pg_store(TEST_URL).connection(True) as db:
            db.execute("UPDATE tasks SET title='changed after transfer' WHERE id=1")
        with self.assertRaisesRegex(migration.TransferError, 'verification failed for tasks'):
            migration.verify_transfer(self.path, TEST_URL)
        with self.pg_store(TEST_URL).connection() as db:
            self.assertEqual(db.execute('SELECT title FROM tasks WHERE id=1').fetchone()['title'], 'changed after transfer')

    def test_empty_source_tables_are_not_reseeded_and_sequences_start_at_one(self):
        path = Path(self.tmp.name) / 'empty.sqlite3'
        source = Store(path)
        source.initialize()
        with source.connection(True) as db:
            db.execute('DELETE FROM employee_aliases')
            db.execute('DELETE FROM employees')
        review = migration.inspect_source(path)
        result = migration.transfer(path, TEST_URL, expected_source_sha256=review['source_data_sha256'])
        self.assertEqual(result['tables']['employees']['rows'], 0)
        target = self.pg_store(TEST_URL)
        target.initialize()
        with target.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM employees').fetchone()['total'], 0)
        with target.connection(True) as db:
            employee_id = target.insert_id(db, "INSERT INTO employees(name,telegram_id,role) VALUES('First',123,'supervisor')")
        self.assertEqual(employee_id, 1)

    def test_cli_apply_and_verify_end_to_end(self):
        for flags, expected_mode in [(['--apply', '--expected-source-sha256', self.review['source_data_sha256']], 'applied'),
                                     (['--verify'], 'verified')]:
            with self.subTest(mode=expected_mode):
                output = io.StringIO()
                with patch.dict(os.environ, {'PLANNER_MIGRATION_DATABASE_URL': TEST_URL}), redirect_stdout(output):
                    self.assertEqual(main(['--source', str(self.path), *flags]), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result['mode'], expected_mode)
                self.assertEqual(result['tables'], self.review['tables'])

    def test_dry_target_inspection_does_not_create_namespace(self):
        self.assertTrue(migration.inspect_target(TEST_URL)['target_empty'])
        self.assert_no_namespace()

    def test_initialized_or_repeated_target_is_never_overwritten(self):
        self.apply()
        target = self.pg_store(TEST_URL)
        target.save_employee(target.actor(699296552), {'name': 'После переноса', 'telegram_id': 8888888888, 'bitrix_id': None, 'active': True})
        with self.assertRaisesRegex(migration.TransferError, 'not empty'):
            self.apply()
        self.assertEqual(target.actor(8888888888)['name'], 'После переноса')

    def test_freshly_initialized_runtime_schema_is_also_refused(self):
        target = self.pg_store(TEST_URL)
        target.initialize()
        before = target.actor(7939114214)
        with self.assertRaisesRegex(migration.TransferError, 'not empty'):
            self.apply()
        self.assertEqual(target.actor(7939114214), before)
        with self.assertRaises(DomainError):
            target.actor(699296552)

    def test_failure_keeps_a_preexisting_empty_namespace(self):
        with self.psycopg.connect(TEST_URL) as db:
            db.execute('CREATE SCHEMA planner')
        original = migration._copy_table
        def broken(source, target, table):
            original(source, target, table)
            if table == 'tasks':
                raise RuntimeError('injected failure')
        with patch.object(migration, '_copy_table', side_effect=broken), self.assertRaises(RuntimeError):
            self.apply()
        with self.psycopg.connect(TEST_URL) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM pg_namespace WHERE nspname='planner'").fetchone())
        self.assertTrue(migration.inspect_target(TEST_URL)['target_empty'])

    def test_stale_review_stops_before_any_destination_changes(self):
        with self.source.connection(True) as db:
            db.execute("UPDATE employees SET name='changed' WHERE id=1")
        self.before = file_hash(self.path)
        with self.assertRaisesRegex(migration.TransferError, 'changed since dry-run'):
            self.apply()
        self.assert_no_namespace()

    def test_copy_failure_rolls_back_schema_rows_and_sequences(self):
        original = migration._copy_table
        def broken(source, target, table):
            original(source, target, table)
            if table == 'audit':
                raise RuntimeError('injected failure')
        with patch.object(migration, '_copy_table', side_effect=broken), self.assertRaises(RuntimeError):
            self.apply()
        self.assert_no_namespace()
        self.assertTrue(self.apply()['verified'])

    def test_same_row_count_content_corruption_rolls_back_everything(self):
        original = migration._verify
        def corrupt(manifest, target):
            target.execute("UPDATE planner.tasks SET title='corrupted'")
            return original(manifest, target)
        with patch.object(migration, '_verify', side_effect=corrupt), self.assertRaisesRegex(migration.TransferError, 'verification failed for tasks'):
            self.apply()
        self.assert_no_namespace()

    def test_sequence_failure_also_rolls_back(self):
        original = migration._restart_identities
        def broken(source, target):
            original(source, target)
            raise RuntimeError('injected after restart')
        with patch.object(migration, '_restart_identities', side_effect=broken), self.assertRaises(RuntimeError):
            self.apply()
        self.assert_no_namespace()

    def test_schema_with_only_a_function_is_not_considered_empty(self):
        with self.psycopg.connect(TEST_URL) as db:
            db.execute('CREATE SCHEMA planner')
            db.execute('CREATE FUNCTION planner.existing() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$')
        with self.assertRaisesRegex(migration.TransferError, 'not empty'):
            self.apply()
        with self.psycopg.connect(TEST_URL) as db:
            self.assertEqual(db.execute('SELECT planner.existing()').fetchone()[0], 1)

    def test_two_concurrent_transfers_only_one_can_commit(self):
        def run(_):
            try:
                return self.apply()['mode']
            except migration.TransferError:
                return 'refused'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(results), ['applied', 'refused'])
