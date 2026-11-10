from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apps.bot.backend.sales_conversion import collect_sales_conversion


class SalesConversionTests(IsolatedAsyncioTestCase):
    async def test_counts_unique_real_transitions_before_period_cutoff(self):
        client = AsyncMock()
        client.list_deal_stage_history.return_value = [{"OWNER_ID": i} for i in range(1, 6)]
        client.get_deals_by_ids.return_value = [
            {"ID": str(i), "ASSIGNED_BY_ID": "7" if i != 5 else "8",
             "CATEGORY_ID": "2" if i == 1 else "0"}
            for i in range(1, 6)
        ]
        client.get_deal_stages.return_value = [
            {"STATUS_ID": "NEW", "NAME": "Новая"}, {"STATUS_ID": "WON", "NAME": "Успех"},
        ]
        entries = [
            (1, "NEW", 0, 9), (1, "WON", 0, 11), (1, "NEW", 0, 12), (1, "WON", 0, 13), (1, "C2:NEW", 2, 14),
            (2, "NEW", 0, 10), (2, "WON", 0, 17),
            (3, "NEW", 0, 10), (3, "C2:NEW", 2, 11), (3, "WON", 0, 12),
        ]
        client.get_deal_stage_history.return_value = [
            {"ID": i, "OWNER_ID": owner, "STAGE_ID": stage, "CATEGORY_ID": category,
             "CREATED_TIME": f"2026-11-{day}T00:00:00+03:00"}
            for i, (owner, stage, category, day) in reversed(list(enumerate(entries, 1)))
        ]
        settings = SimpleNamespace(daily_stats_timezone="Europe/Moscow", track_deal_category_id="0")
        with patch("apps.bot.backend.sales_conversion.get_settings", return_value=settings), patch(
            "apps.bot.backend.sales_conversion.BitrixClient", return_value=client,
        ):
            report = await collect_sales_conversion(7, date(2026, 11, 10), date(2026, 11, 16))

        client.list_deal_stage_history.assert_awaited_once_with({
            "CATEGORY_ID": "0", ">=CREATED_TIME": "2026-11-10T00:00:00+03:00",
            "<CREATED_TIME": "2026-11-17T00:00:00+03:00",
        })
        client.get_deal_stage_history.assert_awaited_once_with(["1", "2", "3", "4"])
        self.assertEqual(report["deal_count"], 4)
        self.assertEqual(report["missing_history"], 1)
        self.assertEqual(report["stages"][0], {
            "name": "Новая", "count": 3,
            "transitions": [{"to": "Успех", "count": 1, "percent": 33.3}],
        })
