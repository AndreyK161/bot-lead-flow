import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import reconcile
from app.bitrix_client import BitrixClient


class BitrixPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_every_list_page(self):
        client = BitrixClient("https://example.test/rest/1/key/")
        client._call_payload = AsyncMock(side_effect=[
            {"result": [{"ID": "1"}], "next": 50},
            {"result": [{"ID": "2"}]},
        ])
        rows = await client._call_all("crm.lead.list", {"filter": {"STATUS_ID": "NEW"}})
        self.assertEqual([row["ID"] for row in rows], ["1", "2"])
        self.assertEqual(client._call_payload.await_args_list[0].args[1]["start"], 0)
        self.assertEqual(client._call_payload.await_args_list[1].args[1]["start"], 50)


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite",
            database_url="",
            reconcile_initial_lookback_hours=24,
            reconcile_overlap_seconds=300,
            reconcile_interval_seconds=300,
            track_deal_category_id="0",
        )
        self.patches = [
            patch("app.store.get_settings", return_value=self.settings),
            patch("app.reconcile.get_settings", return_value=self.settings),
        ]
        for item in self.patches:
            item.start()
        self.client = AsyncMock()
        self.client.get_leads_modified_between.return_value = [{"ID": "1"}]
        self.client.get_deals_modified_between.return_value = [{"ID": "2", "CATEGORY_ID": "0"}]
        self.client_patch = patch("app.reconcile.BitrixClient", return_value=self.client)
        self.client_patch.start()
        self.observe = AsyncMock()
        self.observe_patch = patch("app.reconcile.stats.observe", new=self.observe)
        self.observe_patch.start()

    async def asyncTearDown(self):
        self.observe_patch.stop()
        self.client_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_initial_window_overlap_and_cursor_progress(self):
        end = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
        counts = await reconcile.reconcile_once(end=end)
        self.assertEqual(counts, {"leads": 1, "deals": 1})
        expected_start = (end - timedelta(hours=24, seconds=300)).isoformat()
        self.client.get_leads_modified_between.assert_awaited_once_with(expected_start, end.isoformat())
        self.client.get_deals_modified_between.assert_awaited_once_with("0", expected_start, end.isoformat())
        self.assertEqual(self.observe.await_count, 2)

        next_end = end + timedelta(minutes=5)
        await reconcile.reconcile_once(end=next_end)
        overlap_start = (end - timedelta(seconds=300)).isoformat()
        self.assertEqual(self.client.get_leads_modified_between.await_args_list[-1].args[0], overlap_start)
        self.assertEqual(self.client.get_deals_modified_between.await_args_list[-1].args[1], overlap_start)


if __name__ == "__main__":
    unittest.main()
