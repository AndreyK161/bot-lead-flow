"""Opt-in backup rehearsal against disposable LOCAL Docker containers only.

LEADFLOW_BACKUP_TEST_IMAGE=leadflow-planner-transfer-check:local
python -m unittest discover -s tests -p test_backup_docker.py -v
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

IMAGE = os.environ.get('LEADFLOW_BACKUP_TEST_IMAGE', '')
SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/backup-bot-lead-flow'


@unittest.skipUnless(IMAGE, 'Set LEADFLOW_BACKUP_TEST_IMAGE for disposable Docker backup tests')
class BackupDockerTests(unittest.TestCase):
    @classmethod
    def docker(cls, *args, input=None, check=True):
        return subprocess.run(['docker', '--context', 'desktop-linux', *args], input=input,
                              text=True, capture_output=True, check=check)

    @classmethod
    def setUpClass(cls):
        context = json.loads(cls.docker('context', 'inspect', 'desktop-linux').stdout)[0]
        if not context['Endpoints']['docker']['Host'].startswith('unix://'):
            raise RuntimeError('Backup tests require a local Docker Unix socket')
        suffix = uuid4().hex[:10]
        cls.network = 'lf-backup-test-' + suffix
        cls.pg = cls.network + '-pg'
        cls.sqlite_app = cls.network + '-sqlite'
        cls.postgres_app = cls.network + '-planner'
        cls.invalid_app = cls.network + '-invalid'
        cls.addClassCleanup(cls.cleanup)
        cls.docker('network', 'create', '--internal', cls.network)
        cls.docker('run', '-d', '--name', cls.pg, '--network', cls.network,
                   '--network-alias', 'postgres', '-e', 'POSTGRES_DB=bot_lead_flow',
                   '-e', 'POSTGRES_USER=bot_lead_flow', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
                   'postgres:16-alpine')
        for _ in range(40):
            if cls.docker('exec', cls.pg, 'pg_isready', '-U', 'bot_lead_flow', check=False).returncode == 0:
                break
            time.sleep(.25)
        else:
            raise RuntimeError('Disposable PostgreSQL did not become ready')
        required = {
            'BITRIX_WEBHOOK_URL': 'https://example.invalid/rest/test/',
            'BITRIX_APPLICATION_TOKEN': 'synthetic', 'TELEGRAM_BOT_TOKEN': '1:synthetic',
            'TELEGRAM_CHAT_ID': '1', 'TELEGRAM_WEBHOOK_SECRET': 'synthetic',
            'DIRECTOR_USERS': '1', 'SALES_DEPARTMENT_ID': '1',
            'DATABASE_URL': 'postgresql://bot_lead_flow@postgres/bot_lead_flow',
        }
        for name, planner in [(cls.sqlite_app, ''), (cls.postgres_app, required['DATABASE_URL']),
                              (cls.invalid_app, 'postgresql://bot_lead_flow@postgres/not_the_core_database')]:
            args = ['run', '-d', '--name', name, '--network', cls.network, '--entrypoint', 'sleep']
            for key, value in {**required, 'MANAGER_PLANNER_DATABASE_URL': planner}.items():
                args.extend(['-e', key + '=' + value])
            cls.docker(*args, IMAGE, 'infinity')
        cls.docker('exec', '-i', cls.sqlite_app, 'python', '-', input='''
from pathlib import Path
from apps.planner.backend.store import Store
from apps.planner.backend.postgres import PostgresStore
import psycopg
url = 'postgresql://bot_lead_flow@postgres/bot_lead_flow'
with psycopg.connect(url) as db:
    db.execute('CREATE TABLE public.backup_canary(value text NOT NULL)')
    db.execute("INSERT INTO public.backup_canary VALUES('bot data')")
for store in (Store(Path('/app/data/manager_planner.sqlite3')), PostgresStore(url)):
    store.initialize()
    user = store.actor(7939114214)
    plan = store.create_plan(user, '2026-10-30')
    store.add_task(user, plan['id'], {'title':'Backup test', 'description':'', 'kind':'general', 'planned_minutes':15, 'target':None})
    store.close_plan(user, plan['id'])
''')

    @classmethod
    def cleanup(cls):
        for name in (cls.sqlite_app, cls.postgres_app, cls.invalid_app, cls.pg):
            cls.docker('rm', '-fv', name, check=False)
        cls.docker('network', 'rm', cls.network, check=False)

    def backup(self, directory, app):
        env = {**os.environ, 'DOCKER_CONTEXT': 'desktop-linux',
               'LEADFLOW_BACKUP_DIR': str(directory),
               'LEADFLOW_POSTGRES_CONTAINER': self.pg, 'LEADFLOW_PLANNER_CONTAINER': app}
        return subprocess.run(['sh', str(SCRIPT)], env=env, text=True, capture_output=True)

    def test_sqlite_mode_produces_and_restores_both_archives(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.backup(path, self.sqlite_app)
            self.assertEqual(result.returncode, 0, result.stderr)
            snapshots = list(path.glob('manager-planner-*.sqlite3'))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(len(list(path.glob('bot-lead-flow-*.dump'))), 1)
            with sqlite3.connect(snapshots[0]) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('SELECT count(*) FROM reports').fetchone()[0], 1)
            self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
            self.assertFalse((path / '.bot-lead-flow-backup.lock').exists())

    def test_postgres_archive_restores_planner_and_public_with_report_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.backup(path, self.postgres_app)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Planner PostgreSQL schema planner is included', result.stdout)
            self.assertFalse(list(path.glob('manager-planner-*.sqlite3')))
            archive, = path.glob('bot-lead-flow-*.dump')
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.docker('cp', str(archive), self.pg + ':/tmp/restore-check.dump')
            self.docker('exec', self.pg, 'createdb', '-U', 'bot_lead_flow', 'backup_restore_test')
            self.docker('exec', self.pg, 'pg_restore', '-U', 'bot_lead_flow', '-d', 'backup_restore_test',
                        '--single-transaction', '--exit-on-error', '--no-owner', '--no-privileges', '/tmp/restore-check.dump')
            self.docker('exec', '-i', self.postgres_app, 'python', '-', input='''
import psycopg
from apps.planner.backend.postgres import PostgresStore
store = PostgresStore('postgresql://bot_lead_flow@postgres/backup_restore_test')
store.initialize()
assert store.actor(7939114214)['role'] == 'manager'
with store.connection() as db:
    assert db.execute('SELECT count(*) AS total FROM reports').fetchone()['total'] == 1
with psycopg.connect(store.database_url) as db:
    assert db.execute('SELECT value FROM public.backup_canary').fetchone()[0] == 'bot data'
try:
    with store.connection(True) as db:
        db.execute('DELETE FROM reports')
except psycopg.errors.CheckViolation:
    pass
else:
    raise AssertionError('Immutable report trigger was not restored')
''')

    def test_different_planner_database_is_refused_without_success_or_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.backup(path, self.invalid_app)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('backup created:', result.stdout)
            self.assertNotIn('not_the_core_database', result.stderr)
            self.assertEqual(list(path.iterdir()), [])

    def test_existing_lock_is_not_removed_by_a_second_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            lock = path / '.bot-lead-flow-backup.lock'
            lock.mkdir()
            result = self.backup(path, self.sqlite_app)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(lock.is_dir())
            self.assertFalse(list(path.glob('*.dump')))
