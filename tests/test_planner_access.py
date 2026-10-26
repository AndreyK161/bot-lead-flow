import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import planner_access, store


class PlannerAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch(
            'app.store.get_settings',
            return_value=SimpleNamespace(database_path=self.tmp.name + '/db.sqlite'),
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_seed_imports_roles_without_opening_employee_logins(self):
        planner_access.seed_legacy_identities()
        identities = {row['telegram_id']: row for row in store.list_planner_access()}
        self.assertEqual(len(identities), 6)
        self.assertEqual(identities[1073597301]['role'], 'supervisor')
        self.assertEqual(identities[399005637]['role'], 'manager')
        self.assertEqual(identities[399005637]['display_name'], 'Вячеслав Шандра')
        self.assertEqual(identities[399005637]['bitrix_user_id'], '460')
        self.assertEqual(identities[7874883320]['bitrix_user_id'], '50')
        self.assertEqual(identities[7939114214]['bitrix_user_id'], '110')
        self.assertEqual(identities[8306778724]['bitrix_user_id'], '460')
        self.assertEqual(planner_access.enabled_user_ids(), {1297686797})

    def test_explicit_enable_survives_reseed(self):
        planner_access.seed_legacy_identities()
        store.set_planner_login_enabled(7939114214, True)
        planner_access.seed_legacy_identities()
        self.assertEqual(
            planner_access.enabled_user_ids(role='manager'),
            {7939114214},
        )
        self.assertEqual(
            planner_access.authorized_identity(7939114214)['bitrix_user_id'],
            '110',
        )

    def test_vyacheslav_alias_can_be_explicitly_opened_as_manager(self):
        planner_access.seed_legacy_identities()
        store.set_planner_login_enabled(399005637, True)
        identity = planner_access.authorized_identity(399005637)
        self.assertIsNotNone(identity)
        self.assertEqual(identity['role'], 'manager')
        self.assertEqual(identity['bitrix_user_id'], '460')


if __name__ == '__main__':
    unittest.main()
