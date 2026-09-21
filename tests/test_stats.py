import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import stats, store


class StatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite",
            track_deal_category_id="0",
            lead_unprocessed_status_id="NEW",
            deal_unprocessed_stage_id="NEW",
            daily_stats_time="23:55",
            daily_stats_timezone="Europe/Moscow",
            director_user_id_set={10, 20},
        )
        self.patches = [
            patch("app.store.get_settings", return_value=self.settings),
            patch("app.stats.get_settings", return_value=self.settings),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_reassignment_in_new_changes_owner_then_first_exit_freezes_it(self):
        now = datetime(2026, 9, 21, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        base = {"ID": "1", "STATUS_ID": "NEW", "SOURCE_ID": "WEB", "DATE_CREATE": now.isoformat()}
        stats.record("lead", {**base, "ASSIGNED_BY_ID": "7"}, source_name="Сайт", assignee_name="Анна", now=now)
        stats.record("lead", {**base, "ASSIGNED_BY_ID": "8"}, source_name="Сайт", assignee_name="Борис", now=now)
        processed = stats.record(
            "lead", {**base, "STATUS_ID": "IN_PROCESS", "ASSIGNED_BY_ID": "8"},
            source_name="Сайт", assignee_name="Борис", now=now,
        )
        self.assertEqual(processed["processed_by_id"], "8")
        self.assertEqual(processed["processed_by_name"], "Борис")

        transferred = stats.record(
            "lead", {**base, "STATUS_ID": "IN_PROCESS", "ASSIGNED_BY_ID": "9"},
            source_name="Сайт", assignee_name="Вера", now=now,
        )
        self.assertEqual(transferred["current_assignee_id"], "9")
        self.assertEqual(transferred["processed_by_id"], "8")

    def test_return_to_new_does_not_clear_attribution(self):
        now = datetime(2026, 9, 21, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        item = {"ID": "2", "CATEGORY_ID": "0", "STAGE_ID": "NEW", "ASSIGNED_BY_ID": "7"}
        stats.record("deal", item, assignee_name="Анна", now=now)
        stats.record("deal", {**item, "STAGE_ID": "PREPARATION"}, assignee_name="Анна", now=now)
        returned = stats.record("deal", item, assignee_name="Анна", now=now)
        self.assertEqual(returned["processed_by_id"], "7")
        self.assertIsNotNone(returned["processed_at"])

    async def test_other_deal_pipeline_is_ignored(self):
        client = AsyncMock()
        result = await stats.observe("deal", {"ID": "3", "CATEGORY_ID": "6", "STAGE_ID": "NEW"}, client)
        self.assertIsNone(result)
        self.assertEqual(store.rows("SELECT * FROM crm_items"), [])

    def test_daily_report_groups_sources_and_managers(self):
        now = datetime(2026, 9, 21, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        stats.record("lead", {"ID": "1", "STATUS_ID": "NEW", "SOURCE_ID": "WEB"},
                     source_name="Сайт", assignee_name="Анна", now=now)
        stats.record("deal", {"ID": "2", "STAGE_ID": "PREPARATION", "SOURCE_ID": "WEB", "ASSIGNED_BY_ID": "8"},
                     source_name="Сайт", assignee_name="Борис", now=now)
        report = stats.build_daily_report("2026-09-21")
        self.assertIn("Всего: <b>2</b> (лиды: 1, сделки: 1)", report)
        self.assertIn("Сайт — 2", report)
        self.assertIn("Борис — 1", report)
        self.assertIn("Анна — 1", report)

    async def test_report_is_sent_once_per_director(self):
        send = AsyncMock(side_effect=[{"message_id": 1}, {"message_id": 2}])
        now = datetime(2026, 9, 21, 23, 56, tzinfo=ZoneInfo("Europe/Moscow"))
        with patch("app.stats.send_telegram_message", new=send):
            await stats.send_due_report(now)
            await stats.send_due_report(now)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(len(store.rows("SELECT * FROM daily_report_deliveries")), 2)


if __name__ == "__main__":
    unittest.main()
