from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apps.bot.backend.sales_losses import collect_sales_losses


class SalesLossesTests(IsolatedAsyncioTestCase):
    async def test_uses_stage_before_loss_and_counts_each_deal_once(self):
        client = AsyncMock()
        client.get_deal_stages.return_value = [
            {"STATUS_ID": "NEW", "NAME": "Новая"},
            {"STATUS_ID": "PROPOSAL", "NAME": "Предложение"},
        ]
        client.list_deal_stage_history.side_effect = [
            [{"OWNER_ID": i} for i in (1, 2, 3, 4, 5)],
            [{"OWNER_ID": i} for i in (1, 2, 4)],
        ]
        client.get_deals_by_ids.return_value = [
            {"ID": i, "ASSIGNED_BY_ID": "7" if i != 3 else "8", "CATEGORY_ID": 0,
             "STAGE_ID": "LOSE", "UF_REASON": "9"}
            for i in range(1, 6)
        ]

        def event(id, owner, day, stage, semantic="P", category=0):
            return {"ID": id, "OWNER_ID": owner, "CATEGORY_ID": category, "STAGE_ID": stage,
                    "STAGE_SEMANTIC_ID": semantic, "CREATED_TIME": f"2026-11-{day:02}T12:00:00+03:00"}

        client.get_deal_stage_history.return_value = list(reversed([
            event(1, 1, 8, "NEW"),
            event(2, 1, 10, "LOSE", "F"),
            event(3, 1, 11, "PROPOSAL"),
            event(4, 1, 12, "UC_8IETDG"),
            event(5, 2, 9, "NEW"),
            event(6, 2, 10, "LOSE", "F"),
            event(7, 3, 11, "LOSE", "F"),
            event(8, 4, 9, "NEW"),
            event(9, 4, 10, "C2:NEW", category=2),
            event(10, 4, 11, "LOSE", "F"),
            event(11, 5, 9, "LOSE", "F"),
            event(12, 5, 10, "LOSE", "F"),
            event(13, 2, 18, "PROPOSAL"),
            event(14, 2, 19, "LOSE", "F"),
        ]))
        settings = SimpleNamespace(
            track_deal_category_id="0", deal_refusal_reason_field="UF_REASON",
            daily_stats_timezone="Europe/Moscow",
        )
        with patch("apps.bot.backend.sales_losses.get_settings", return_value=settings), patch(
            "apps.bot.backend.sales_losses.BitrixClient", return_value=client,
        ):
            report = await collect_sales_losses(7, date(2026, 11, 10), date(2026, 11, 16))

        client.get_deal_stage_history.assert_awaited_once_with(["1", "2", "4", "5"])
        self.assertEqual(report, {"count": 3, "stages": [
            {"name": "Новая", "count": 1, "reasons": [{"name": "Историческая причина неизвестна", "count": 1}]},
            {"name": "Предложение", "count": 1, "reasons": [{"name": "Брак", "count": 1}]},
            {"name": "Предыдущая стадия неизвестна", "count": 1, "reasons": [{"name": "Историческая причина неизвестна", "count": 1}]},
        ]})
