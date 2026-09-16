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

    def test_link_manager_overwrites_previous_link(self):
        store.link_manager('460', 'Никита', 555)
        store.link_manager('460', 'Никита', 777)
        self.assertEqual(store.manager_telegram_id('460'), 777)
        self.assertEqual(len(store.list_links()), 1)

    def test_list_links_sorted_by_name(self):
        store.link_manager('2', 'Борис', 2)
        store.link_manager('1', 'Анна', 1)
        names = [link['bitrix_name'] for link in store.list_links()]
        self.assertEqual(names, ['Анна', 'Борис'])


if __name__ == '__main__':
    unittest.main()
