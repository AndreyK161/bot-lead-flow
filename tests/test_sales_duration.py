from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apps.bot.backend.sales_duration import collect_sales_duration


class SalesDurationTests(IsolatedAsyncioTestCase):
    async def test_uses_first_success_in_period_and_includes_deals_created_earlier(self):
        client = AsyncMock()
        client.get_deal_stages.return_value = [{"STATUS_ID": "WON"}]
        client.get_deals_by_ids.return_value = [
            {"ID": 1, "ASSIGNED_BY_ID": "7", "DATE_CREATE": "2026-11-01T12:00:00+03:00"},
            {"ID": 2, "ASSIGNED_BY_ID": "7", "DATE_CREATE": "2026-11-01T09:00:00+03:00"},
            {"ID": 3, "ASSIGNED_BY_ID": "8", "DATE_CREATE": "2026-11-01T09:00:00+03:00"},
            {"ID": 4, "ASSIGNED_BY_ID": "7", "DATE_CREATE": None},
        ]

        def win(deal_id, at):
            return {"OWNER_ID": deal_id, "CREATED_TIME": at}

        client.list_deal_stage_history.side_effect = [
            [win(i, "2026-11-11T21:00:00Z") for i in (1, 2, 3, 4)],
            [
                win(1, "2026-11-13T00:00:00+03:00"),
                win(1, "2026-11-11T21:00:00Z"),
                win(2, "2026-11-09T23:59:00+03:00"),
                win(2, "2026-11-11T21:00:00Z"),
                win(4, "2026-11-11T21:00:00Z"),
            ],
        ]
        settings = SimpleNamespace(daily_stats_timezone="Europe/Moscow", track_deal_category_id="0")
        with patch("apps.bot.backend.sales_duration.get_settings", return_value=settings), patch(
            "apps.bot.backend.sales_duration.BitrixClient", return_value=client,
        ):
            report = await collect_sales_duration(7, date(2026, 11, 10), date(2026, 11, 16))

        self.assertEqual(client.list_deal_stage_history.await_args_list[0].args[0], {
            "CATEGORY_ID": "0", "STAGE_ID": "WON",
            ">=CREATED_TIME": "2026-11-10T00:00:00+03:00",
            "<CREATED_TIME": "2026-11-17T00:00:00+03:00",
        })
        self.assertEqual(client.list_deal_stage_history.await_args.args[0]["@OWNER_ID"], ["1", "2", "4"])
        self.assertEqual(report, {
            "count": 1, "missing_dates": 1, "average_days": 10.5, "median_days": 10.5,
            "deals": [{
                "deal_id": "1", "created_at": "2026-11-01T12:00:00+03:00",
                "won_at": "2026-11-12T00:00:00+03:00", "days": 10.5,
            }],
        })
