import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from apps.bot.backend import store as core
from apps.planner.backend.store import Store
from apps.planner.backend.sync import outbox_sync_once
from apps.shared import planner_outbox


class PlannerOutboxTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'planner.sqlite3'
        settings = SimpleNamespace(database_url='', database_path=str(Path(directory) / 'core.sqlite3'))
        self.enterContext(patch('apps.bot.backend.store.get_settings', return_value=settings))
        self.planner = Store(self.path)
        self.snapshot = {
            'schema': 1, 'distribution_id': str(uuid4()), 'workday': '2026-10-21',
            'confirmed_at': '2026-10-21T10:00:00+03:00', 'confirmed_by': 1297686797,
            'variant_index': 0, 'variant_id': 'test', 'stage_id': 'IN_PROCESS', 'total': 1,
            'allocations': [{'telegram_id': 7939114214, 'bitrix_id': 110, 'name': 'Яна',
                             'dates': ['2026-10-20'], 'count': 1,
                             'leads': [{'id': 10, 'created_date': '2026-10-20', 'stage_id': 'IN_PROCESS'}]}],
        }
        self.allowed = {1297686797}

    def publish(self):
        return planner_outbox.publish(self.snapshot, self.allowed)

    def task_count(self):
        with self.planner.connection() as db:
            return db.execute('SELECT count(*) FROM tasks').fetchone()[0]

    def test_bot_can_publish_while_planner_is_offline_and_retries_do_not_duplicate(self):
        self.assertEqual(self.publish()['status'], 'pending')
        self.assertEqual(self.publish()['status'], 'pending')
        self.assertFalse(self.path.exists())
        self.planner.initialize()
        self.assertEqual(outbox_sync_once(self.planner, self.allowed), ['imported'])
        self.assertEqual(self.task_count(), 1)
        self.assertEqual(outbox_sync_once(self.planner, self.allowed), [])
        self.assertEqual(self.publish()['status'], 'imported')

    def test_repeated_confirmation_preserves_original_timestamp_and_payload(self):
        first = self.publish()
        repeated = deepcopy(self.snapshot)
        repeated['confirmed_at'] = '2026-10-21T10:01:00+03:00'
        self.assertEqual(planner_outbox.publish(repeated, self.allowed), first)
        conflicting = deepcopy(repeated)
        conflicting['allocations'][0]['name'] = 'Different employee'
        with self.assertRaises(ValueError):
            planner_outbox.publish(conflicting, self.allowed)

    def test_failure_between_local_commit_and_acknowledgement_is_safe_to_retry(self):
        self.publish()
        self.planner.initialize()
        with patch('apps.shared.planner_outbox.acknowledge', side_effect=RuntimeError('synthetic crash')):
            self.assertEqual(outbox_sync_once(self.planner, self.allowed), ['error'])
        self.assertEqual(self.task_count(), 1)
        self.assertEqual(len(planner_outbox.pending()), 1)
        self.assertEqual(outbox_sync_once(Store(self.path), self.allowed), ['imported'])
        self.assertEqual(self.task_count(), 1)
        self.assertEqual(planner_outbox.pending(), [])

    def test_conflicting_confirmation_and_unauthorized_publisher_are_rejected(self):
        with self.assertRaises(PermissionError):
            planner_outbox.publish(self.snapshot, set())
        self.publish()
        other = deepcopy(self.snapshot)
        other['distribution_id'] = str(uuid4())
        with self.assertRaises(ValueError):
            planner_outbox.publish(other, self.allowed)
        self.assertEqual(len(planner_outbox.pending()), 1)

    def test_closed_plan_is_preserved_and_message_is_acknowledged_for_review(self):
        self.planner.initialize()
        user = self.planner.actor(7939114214)
        plan = self.planner.create_plan(user, self.snapshot['workday'])
        before = self.planner.close_plan(user, plan['id'])
        self.publish()
        self.assertEqual(outbox_sync_once(self.planner, self.allowed), ['review'])
        self.assertEqual(self.planner.report(user, plan['id']), before)
        self.assertEqual(self.task_count(), 0)
        self.assertEqual(self.publish()['status'], 'review')
