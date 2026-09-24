import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import outcomes, stats, store


TEST_DIRECTOR_ID = 1297686797


class OutcomeTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite",
            database_url="",
            track_deal_category_id="0",
            accompaniment_deal_category_id="2",
            contract_source_url_field="UF_CRM_CONTRACT_URL",
            lead_unprocessed_status_id="NEW",
            deal_unprocessed_stage_id="NEW",
            daily_stats_timezone="Europe/Moscow",
            outcome_sync_time="03:00",
            director_user_id_set={TEST_DIRECTOR_ID},
        )
        self.patches = [
            patch("app.store.get_settings", return_value=self.settings),
            patch("app.stats.get_settings", return_value=self.settings),
            patch("app.outcomes.get_settings", return_value=self.settings),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_daily_sync_links_lead_deal_and_contract(self):
        created = datetime(2026, 9, 20, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        stats.record(
            "lead",
            {"ID": "10", "STATUS_ID": "NEW", "DATE_CREATE": created.isoformat()},
            now=created,
        )
        stats.record(
            "deal",
            {
                "ID": "20", "CATEGORY_ID": "0", "STAGE_ID": "NEW", "LEAD_ID": "10",
                "DATE_CREATE": "2026-09-20T11:00:00+03:00",
            },
            now=created,
        )

        client = AsyncMock()
        client.get_lead_statuses.return_value = [
            {"STATUS_ID": "NEW", "NAME": "Не обработан"},
            {"STATUS_ID": "CONVERTED", "NAME": "Качественный лид"},
        ]
        client.get_deal_stages.side_effect = [
            [{"STATUS_ID": "WON", "NAME": "Сделка успешна"}],
            [{"STATUS_ID": "C2:NEW", "NAME": "Новый договор"}],
        ]
        client.get_leads_by_ids.return_value = [{
            "ID": "10", "STATUS_ID": "CONVERTED", "CONTACT_ID": "500",
            "DATE_CREATE": created.isoformat(), "DATE_MODIFY": "2026-09-21T12:00:00+03:00",
        }]
        sale = {
            "ID": "20", "TITLE": "Клиент", "CATEGORY_ID": "0", "STAGE_ID": "WON",
            "LEAD_ID": "10", "CONTACT_ID": "500", "DATE_CREATE": "2026-09-20T11:00:00+03:00",
            "DATE_MODIFY": "2026-09-21T12:00:00+03:00",
        }
        client.get_deals_by_ids.side_effect = [[sale], []]
        client.get_deals_by_lead_ids.return_value = [sale]
        client.get_deals_by_contact_ids.return_value = [sale]
        client.get_category_deals_after_id.return_value = [{
            "ID": "30", "TITLE": "Клиент", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW",
            "CONTACT_ID": "500", "DATE_CREATE": "2026-09-21T13:00:00+03:00",
            "UF_CRM_CONTRACT_URL": "https://example.test/dogovor/20/?token=hidden",
        }]

        result = await outcomes.sync_once(
            now=datetime(2026, 9, 24, 3, tzinfo=ZoneInfo("Europe/Moscow")), client=client,
        )

        self.assertEqual(result, {"items": 2, "converted": 1, "contracts": 2})
        lead = store.rows("SELECT * FROM crm_items WHERE entity_type='lead' AND entity_id='10'")[0]
        deal = store.rows("SELECT * FROM crm_items WHERE entity_type='deal' AND entity_id='20'")[0]
        self.assertEqual(lead["current_stage_name"], "Качественный лид")
        self.assertEqual(lead["linked_deal_id"], "20")
        self.assertEqual(lead["contract_deal_id"], "30")
        self.assertEqual(lead["outcome_stage_name"], "Новый договор")
        self.assertEqual(deal["origin_lead_id"], "10")
        self.assertEqual(deal["contract_deal_id"], "30")

        report = outcomes.build_period_report(date(2026, 9, 20), date(2026, 9, 20))
        self.assertIn("Обращений: <b>1</b>", report)
        self.assertIn("Перешли в сделку: <b>1</b> · договоры: <b>1</b>", report)
        self.assertIn("Договор заключён", report)

    async def test_missing_lead_id_falls_back_to_unique_contact(self):
        created = datetime(2026, 9, 20, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        stats.record("lead", {"ID": "10", "STATUS_ID": "NEW", "DATE_CREATE": created.isoformat()}, now=created)
        stats.record(
            "deal",
            {"ID": "20", "CATEGORY_ID": "0", "STAGE_ID": "NEW", "DATE_CREATE": created.isoformat()},
            now=created,
        )
        client = AsyncMock()
        client.get_lead_statuses.return_value = [{"STATUS_ID": "CONVERTED", "NAME": "Качественный лид"}]
        client.get_deal_stages.side_effect = [
            [{"STATUS_ID": "WON", "NAME": "Сделка успешна"}],
            [{"STATUS_ID": "C2:NEW", "NAME": "Новый договор"}],
        ]
        client.get_leads_by_ids.return_value = [{
            "ID": "10", "STATUS_ID": "CONVERTED", "CONTACT_ID": "500",
            "DATE_CREATE": created.isoformat(), "DATE_MODIFY": "2026-09-21T12:00:00+03:00",
        }]
        sale = {
            "ID": "20", "CATEGORY_ID": "0", "STAGE_ID": "WON", "LEAD_ID": None,
            "CONTACT_ID": "500", "DATE_CREATE": "2026-09-20T11:00:00+03:00",
            "DATE_MODIFY": "2026-09-21T12:00:00+03:00",
        }
        client.get_deals_by_ids.side_effect = [[sale], []]
        client.get_deals_by_lead_ids.return_value = []
        client.get_deals_by_contact_ids.return_value = [sale]
        client.get_category_deals_after_id.return_value = []

        await outcomes.sync_once(now=datetime(2026, 9, 24, 3, tzinfo=ZoneInfo("Europe/Moscow")), client=client)

        lead = store.rows("SELECT * FROM crm_items WHERE entity_type='lead' AND entity_id='10'")[0]
        deal = store.rows("SELECT * FROM crm_items WHERE entity_type='deal' AND entity_id='20'")[0]
        self.assertEqual(lead["linked_deal_id"], "20")
        self.assertEqual(deal["origin_lead_id"], "10")

    def test_period_parser_accepts_required_format(self):
        self.assertEqual(
            outcomes.parse_period("/period 01.09.2026 - 23.09.2026"),
            (date(2026, 9, 1), date(2026, 9, 23)),
        )
        self.assertIsNone(outcomes.parse_period("/period 23.09.2026 - 01.09.2026"))
        self.assertIsNone(outcomes.parse_period("/period 2026-09-01 - 2026-09-23"))

    def test_contract_link_has_priority_over_ambiguous_titles(self):
        sales = [
            {"ID": "20", "TITLE": "Одинаковый клиент", "DATE_CREATE": "2026-09-01T10:00:00+03:00"},
            {"ID": "21", "TITLE": "Одинаковый клиент", "DATE_CREATE": "2026-09-01T10:00:00+03:00"},
        ]
        contract = {
            "ID": "30", "TITLE": "Одинаковый клиент", "DATE_CREATE": "2026-09-02T10:00:00+03:00",
            "UF_CRM_CONTRACT_URL": "https://example.test/dogovor/21/",
        }
        matched = outcomes._match_contracts([contract], sales, "UF_CRM_CONTRACT_URL")
        self.assertEqual(matched["21"]["ID"], "30")


if __name__ == "__main__":
    unittest.main()
