from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apps.bot.backend.contracts_report import collect_contracts_report


class ContractsReportTests(IsolatedAsyncioTestCase):
    async def test_counts_by_unified_source_and_keeps_currencies_separate(self):
        client = AsyncMock()
        client.get_sources.return_value = [{"STATUS_ID": "WEB", "NAME": "Сайт"}]

        client.get_deal_stages.return_value = [{"STATUS_ID": "C2:LOSE", "SEMANTICS": "F"}]
        client.get_deals_by_ids.return_value = [
            {"ID": i + 100, "ASSIGNED_BY_ID": "8" if i == 4 else "7",
             "SOURCE_ID": "" if i == 6 else "WEB"} for i in range(1, 8)
        ]

        def contract(id, amount, currency="RUB", **fields):
            return {
                "ID": id, "ASSIGNED_BY_ID": "99", "SOURCE_ID": "OTHER",
                "UF_LINK": f"https://example.test/dogovor/{id + 100}/",
                "DATE_CREATE": "2026-10-01", "UF_DATE": "2026-11-10",
                "OPPORTUNITY": amount, "CURRENCY_ID": currency,
            } | fields

        first = contract(1, "10.10")
        client.get_contracts_for_report.return_value = [
            first, first, contract(2, "0.20", UF_DATE=None, DATE_CREATE="2026-11-10"),
            contract(3, "5", "USD"), contract(4, "100", ASSIGNED_BY_ID="8"),
            contract(5, "100", UF_DATE="2026-11-17", DATE_CREATE="2026-11-10"),
            contract(6, None, UTM_SOURCE="unrelated"),
            contract(7, "500", STAGE_ID="C2:LOSE"), contract(8, "500", UF_LINK=None),
        ]
        settings = SimpleNamespace(
            daily_stats_timezone="Europe/Moscow", deal_contract_date_field="UF_DATE",
            accompaniment_deal_category_id="2", contract_source_url_field="UF_LINK",
        )
        with patch("apps.bot.backend.contracts_report.get_settings", return_value=settings), patch(
            "apps.bot.backend.contracts_report.BitrixClient", return_value=client,
        ):
            report = await collect_contracts_report(7, date(2026, 11, 10), date(2026, 11, 16))

        client.get_contracts_for_report.assert_awaited_once_with(
            "2", "UF_DATE", "2026-11-10T00:00:00+03:00", "2026-11-17T00:00:00+03:00",
            source_field="UF_LINK",
        )
        self.assertEqual(report, {
            "count": 4, "missing_amount": 1, "revenue": {"RUB": "10.30", "USD": "5"},
            "sources": [
                {"name": "Сайт", "count": 3, "missing_amount": 0, "revenue": {"RUB": "10.30", "USD": "5"}},
                {"name": "Без источника", "count": 1, "missing_amount": 1, "revenue": {}},
            ],
        })
