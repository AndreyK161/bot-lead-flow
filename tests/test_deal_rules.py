import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import deal_rules, stats


class DealRulesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite",
            database_url="",
            track_deal_category_id="0",
            accompaniment_deal_category_id="2",
            deal_unprocessed_stage_id="NEW",
            deal_refusal_reason_field="UF_CRM_REFUSAL_REASON_OP",
            deal_contract_date_field="UF_CRM_CONTRACT_DATE_OP",
            daily_stats_timezone="Europe/Moscow",
        )
        self.patches = [
            patch("app.store.get_settings", return_value=self.settings),
            patch("app.stats.get_settings", return_value=self.settings),
            patch("app.deal_rules.get_settings", return_value=self.settings),
        ]
        for item in self.patches:
            item.start()
        deal_rules._reason_item_cache = None

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_refusal_stage_populates_export_field(self):
        client = AsyncMock()
        client.get_deal_userfields.return_value = [{
            "FIELD_NAME": "UF_CRM_REFUSAL_REASON_OP",
            "LIST": [{"ID": "501", "VALUE": "Нет смысла звонить"}],
        }]
        deal = {"ID": "10", "CATEGORY_ID": "0", "STAGE_ID": "UC_NPZOBZ"}

        result = await deal_rules.apply_refusal_rule(deal, client)

        client.update_deal.assert_awaited_once_with(
            "10", {"UF_CRM_REFUSAL_REASON_OP": "501"},
        )
        self.assertEqual(result["UF_CRM_REFUSAL_REASON_OP"], "501")

    async def test_generic_loss_without_reason_is_reverted(self):
        stats.record("deal", {"ID": "11", "CATEGORY_ID": "0", "STAGE_ID": "EXECUTING"})
        client = AsyncMock()

        result = await deal_rules.apply_refusal_rule(
            {"ID": "11", "CATEGORY_ID": "0", "STAGE_ID": "LOSE"}, client,
        )

        client.update_deal.assert_awaited_once_with("11", {"STAGE_ID": "EXECUTING"})
        self.assertEqual(result["STAGE_ID"], "EXECUTING")

    async def test_generic_loss_with_reason_is_kept(self):
        client = AsyncMock()
        deal = {
            "ID": "12", "CATEGORY_ID": "0", "STAGE_ID": "LOSE",
            "UF_CRM_REFUSAL_REASON_OP": "502",
        }
        self.assertEqual(await deal_rules.apply_refusal_rule(deal, client), deal)
        client.update_deal.assert_not_awaited()

    async def test_new_accompaniment_deal_uses_bitrix_creation_time(self):
        client = AsyncMock()
        deal = {
            "ID": "30", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW",
            "DATE_CREATE": "2026-10-19T14:25:00+03:00",
        }

        result = await deal_rules.apply_event_rules(deal, client, created=True)

        client.update_deal.assert_awaited_once_with(
            "30", {"UF_CRM_CONTRACT_DATE_OP": "2026-10-19T14:25:00+03:00"},
        )
        self.assertEqual(result["UF_CRM_CONTRACT_DATE_OP"], "2026-10-19T14:25:00+03:00")

    async def test_update_in_accompaniment_does_not_backfill_old_deal(self):
        client = AsyncMock()
        deal = {"ID": "31", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW"}
        self.assertEqual(
            await deal_rules.apply_event_rules(deal, client, created=False), deal,
        )
        client.update_deal.assert_not_awaited()

    async def test_move_from_sales_to_accompaniment_uses_modify_time(self):
        stats.record("deal", {"ID": "32", "CATEGORY_ID": "0", "STAGE_ID": "WON"})
        client = AsyncMock()
        moved = {
            "ID": "32", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW",
            "DATE_MODIFY": "2026-10-19T16:40:00+03:00",
        }

        result = await deal_rules.apply_event_rules(moved, client, created=False)

        client.update_deal.assert_awaited_once_with(
            "32", {"UF_CRM_CONTRACT_DATE_OP": "2026-10-19T16:40:00+03:00"},
        )
        self.assertEqual(result["UF_CRM_CONTRACT_DATE_OP"], "2026-10-19T16:40:00+03:00")
