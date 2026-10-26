import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook

from app import client_registry


class ClientRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_active_and_completed_clients_and_excludes_failed(self):
        settings = SimpleNamespace(
            accompaniment_deal_category_id="2",
            deal_contract_date_field="UF_CONTRACT_DATE",
            bitrix_webhook_url="https://example.bitrix24.ru/rest/1/token/",
            daily_stats_timezone="Europe/Moscow",
        )
        client = AsyncMock()
        client.get_category_deals.return_value = [
            {
                "ID": "12", "TITLE": "Fallback", "CONTACT_ID": "102", "STAGE_ID": "C2:WON",
                "DATE_CREATE": "2026-01-01T10:00:00+03:00",
                "UF_CONTRACT_DATE": "2026-10-18T14:00:00+03:00",
            },
            {
                "ID": "11", "TITLE": "Fallback", "CONTACT_ID": "101", "STAGE_ID": "C2:NEW",
                "DATE_CREATE": "2026-10-17T10:00:00+03:00", "UF_CONTRACT_DATE": "",
            },
            {"ID": "10", "CONTACT_ID": "100", "STAGE_ID": "C2:LOSE"},
        ]
        client.get_deal_stages.return_value = [
            {"STATUS_ID": "C2:NEW", "NAME": "Новый договор", "SEMANTICS": "P"},
            {"STATUS_ID": "C2:WON", "NAME": "Успешно", "SEMANTICS": "S"},
            {"STATUS_ID": "C2:LOSE", "NAME": "Расторгнут", "SEMANTICS": "F"},
        ]
        client.get_contacts_full_by_ids.return_value = [
            {
                "ID": "101", "NAME": "Иван", "SECOND_NAME": "Иванович", "LAST_NAME": "Иванов",
                "PHONE": [{"VALUE": "+79990001122"}],
            },
            {"ID": "102", "NAME": "Анна", "LAST_NAME": "Петрова", "PHONE": []},
        ]

        with patch("app.client_registry.get_settings", return_value=settings):
            data = await client_registry.collect_clients(client=client)

        self.assertEqual([item["deal_id"] for item in data["items"]], ["12", "11"])
        self.assertEqual(data["items"][1]["name"], "Иванов Иван Иванович")
        self.assertEqual(data["items"][1]["phone"], "+79990001122")
        self.assertEqual(data["items"][1]["contract_date"], "2026-10-17T10:00:00+03:00")
        self.assertEqual(data["totals"], {"all": 2, "active": 1, "completed": 1, "without_phone": 1})
        self.assertEqual(data["items"][0]["deal_url"], "https://example.bitrix24.ru/crm/deal/details/12/")

    def test_excel_contains_required_columns_and_safe_phone(self):
        content = client_registry.build_clients_workbook({"items": [{
            "name": "Иванов Иван", "phone": "+79990001122", "stage": "В работе",
            "contract_date": "2026-10-18T14:00:00+03:00", "status": "active",
            "deal_id": "42", "deal_url": "https://example.bitrix24.ru/crm/deal/details/42/",
        }]})
        sheet = load_workbook(BytesIO(content), data_only=False)["Клиенты"]
        self.assertEqual([cell.value for cell in sheet[1]], [
            "ФИО", "Телефон", "Стадия сопровождения", "Дата договора", "Статус", "Сделка",
        ])
        self.assertEqual(sheet["B2"].value, "'+79990001122")
        self.assertEqual(sheet["B2"].number_format, "@")
        self.assertEqual(sheet["D2"].number_format, "DD.MM.YYYY HH:MM")
        self.assertEqual(sheet["F2"].hyperlink.target, "https://example.bitrix24.ru/crm/deal/details/42/")


if __name__ == "__main__":
    unittest.main()
