import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import store


class StoreLinkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        settings = SimpleNamespace(database_path=self.tmp.name + '/db.sqlite')
        self.patcher = patch('app.store.get_settings', return_value=settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_record_start_then_recent_starts_returns_it(self):
        store.record_start(555, 'ivan', 'Иван')
        starts = store.recent_starts()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['telegram_id'], 555)
        self.assertEqual(starts[0]['username'], 'ivan')

    def test_record_start_upserts_on_repeat(self):
        store.record_start(555, 'ivan', 'Иван')
        store.record_start(555, 'ivan_new', 'Иван')
        starts = store.recent_starts()
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['username'], 'ivan_new')

    def test_link_manager_then_manager_telegram_id(self):
        store.link_manager('460', 'Никита', 555)
        self.assertEqual(store.manager_telegram_id('460'), 555)

    def test_manager_telegram_id_missing_returns_none(self):
        self.assertIsNone(store.manager_telegram_id('nonexistent'))

    def test_manager_can_have_multiple_telegram_accounts(self):
        store.link_manager('460', 'Никита', 555)
        store.link_manager('460', 'Никита', 777)
        self.assertEqual(store.manager_telegram_ids('460'), [555, 777])
        self.assertEqual(len(store.list_links()), 2)

    def test_telegram_account_is_reassigned_instead_of_duplicated(self):
        store.link_manager('460', 'Никита', 555)
        store.link_manager('777', 'Анна', 555)
        self.assertEqual(store.manager_telegram_ids('460'), [])
        self.assertEqual(store.manager_telegram_ids('777'), [555])

    def test_legacy_single_account_table_is_migrated(self):
        path = self.tmp.name + '/db.sqlite'
        connection = sqlite3.connect(path)
        connection.execute(
            'CREATE TABLE manager_links ('
            'bitrix_user_id TEXT PRIMARY KEY, bitrix_name TEXT, telegram_id BIGINT NOT NULL, '
            'linked_at TEXT DEFAULT CURRENT_TIMESTAMP)'
        )
        connection.execute(
            'INSERT INTO manager_links (bitrix_user_id,bitrix_name,telegram_id) VALUES (?,?,?)',
            ('460', 'Никита', 555),
        )
        connection.commit()
        connection.close()

        store.link_manager('460', 'Никита', 777)

        self.assertEqual(store.manager_telegram_ids('460'), [555, 777])

    def test_list_links_sorted_by_name(self):
        store.link_manager('2', 'Борис', 2)
        store.link_manager('1', 'Анна', 1)
        names = [link['bitrix_name'] for link in store.list_links()]
        self.assertEqual(names, ['Анна', 'Борис'])

    def test_ui_mode_defaults_to_supervisor_and_persists_manager(self):
        self.assertEqual(store.user_ui_mode(1297686797), 'supervisor')
        store.set_user_ui_mode(1297686797, 'manager')
        self.assertEqual(store.user_ui_mode(1297686797), 'manager')

    def test_ui_mode_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            store.set_user_ui_mode(1297686797, 'admin')

    def test_distribution_session_round_trip_and_clear(self):
        store.save_distribution_session(1297686797, 'abc123', 'managers', {'selected': [1]})
        session = store.distribution_session(1297686797)
        self.assertEqual(session['session_id'], 'abc123')
        self.assertEqual(session['step'], 'managers')
        self.assertEqual(session['payload'], {'selected': [1]})
        store.clear_distribution_session(1297686797)
        self.assertIsNone(store.distribution_session(1297686797))

    def test_distribution_reminder_claim_is_idempotent_per_day(self):
        store.set_distribution_reminder(1297686797, 1297686797, True)
        self.assertEqual(len(store.distribution_reminders()), 1)
        self.assertTrue(store.claim_distribution_reminder(1297686797, 0, '2026-10-21'))
        self.assertFalse(store.claim_distribution_reminder(1297686797, 0, '2026-10-21'))
        self.assertTrue(store.claim_distribution_reminder(1297686797, 0, '2026-10-22'))
        store.set_distribution_reminder(1297686797, 1297686797, False)
        self.assertEqual(store.distribution_reminders(), [])

    def test_planner_access_upsert_preserves_explicit_login_switch(self):
        values = {
            'telegram_id': 42, 'bitrix_user_id': '110', 'display_name': 'Яна',
            'role': 'manager', 'active': True, 'source': 'legacy',
        }
        store.upsert_planner_access(**values, login_enabled_on_insert=False)
        self.assertFalse(store.planner_access(42)['login_enabled'])
        self.assertTrue(store.set_planner_login_enabled(42, True))
        store.upsert_planner_access(**values, login_enabled_on_insert=False)
        self.assertTrue(store.planner_access(42)['login_enabled'])

    def test_planner_access_rejects_unknown_role(self):
        with self.assertRaises(ValueError):
            store.upsert_planner_access(
                42, bitrix_user_id=None, display_name='X', role='admin',
                active=True, source='test',
            )

    def test_seen_lead_can_be_found_by_normalized_phone(self):
        store.record_seen_lead({'ID':'20462','PHONE':[{'VALUE':'+7 (952) 123-45-67'}]})
        self.assertTrue(store.was_lead_seen('20462'))
        self.assertEqual(store.find_seen_lead_by_phones(['8 952 123 45 67']),'20462')

    def test_unknown_phone_does_not_create_false_relation(self):
        store.record_seen_lead({'ID':'1','PHONE':[{'VALUE':'+7 900 111-22-33'}]})
        self.assertIsNone(store.find_seen_lead_by_phones(['+7 900 999-88-77']))

    def test_plus7_and_leading_8_are_treated_as_the_same_number(self):
        store.record_seen_lead({'ID': '20462', 'PHONE': [{'VALUE': '+79241643860'}]})
        self.assertEqual(store.find_all_seen_lead_ids_by_phones(['+89241643860']), ['20462'])
        self.assertEqual(store.find_all_seen_lead_ids_by_phones(['89241643860']), ['20462'])
        self.assertEqual(store.find_all_seen_lead_ids_by_phones(['+7 (924) 164-38-60']), ['20462'])
        self.assertEqual(store.find_all_seen_lead_ids_by_phones(['9241643860']), ['20462'])

    def test_find_all_seen_lead_ids_returns_every_match_not_just_latest(self):
        store.record_seen_lead({'ID': '1', 'PHONE': [{'VALUE': '+79241643860'}]})
        store.record_seen_lead({'ID': '2', 'PHONE': [{'VALUE': '89241643860'}]})
        self.assertEqual(set(store.find_all_seen_lead_ids_by_phones(['9241643860'])), {'1', '2'})

    def test_find_all_seen_lead_ids_empty_without_phones(self):
        self.assertEqual(store.find_all_seen_lead_ids_by_phones([]), [])
        self.assertEqual(store.find_all_seen_lead_ids_by_phones([None, '']), [])


if __name__ == '__main__':
    unittest.main()
