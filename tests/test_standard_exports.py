import tempfile
import unittest
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from app import standard_exports, store


class FakeClient:
    async def get_leads_created_between_full(self, start, end):
        return [{
            "ID": "10", "DATE_CREATE": "2026-10-02T10:00:00+03:00",
            "ASSIGNED_BY_ID": "110", "STATUS_ID": "IN_PROCESS", "SOURCE_ID": "WEB",
            "PHONE": [{"VALUE": "+7 900 000-00-01"}],
        }]

    async def get_deals_created_between_full(self, category, start, end, extra_fields=None):
        return [{
            "ID": "20", "DATE_CREATE": "2026-10-03T10:00:00+03:00",
            "DATE_MODIFY": "2026-10-04T11:00:00+03:00", "ASSIGNED_BY_ID": "110",
            "STAGE_ID": "LOSE", "SOURCE_ID": "WEB", "CONTACT_ID": "30",
            "OPPORTUNITY": "100000", "CURRENCY_ID": "RUB", "UF_REASON": "501",
        }]

    async def get_deals_modified_between(self, category, start, end):
        return [{"ID": "20"}]

    async def get_sources(self):
        return [{"STATUS_ID": "WEB", "NAME": "Сайт"}]

    async def get_lead_statuses(self):
        return [{"STATUS_ID": "IN_PROCESS", "NAME": "В работе"}]

    async def get_deal_stages(self, category):
        return [{"STATUS_ID": "LOSE", "NAME": "Отказ"}]

    async def get_deal_userfields(self):
        return [{"FIELD_NAME": "UF_REASON", "LIST": [{"ID": "501", "VALUE": "Мусор"}]}]

    async def get_deals_by_ids(self, ids, select=None):
        return []

    async def get_contacts_full_by_ids(self, ids):
        return [{"ID": "30", "PHONE": [{"VALUE": "+7 900 000-00-02"}]}]

    async def get_deal_stage_history(self, ids):
        return [{
            "OWNER_ID": "20", "CATEGORY_ID": "0", "STAGE_ID": "LOSE",
            "CREATED_TIME": "2026-10-04T10:55:00+03:00",
        }]

    async def get_user_name(self, user_id):
        return "Яна Азизова"


class StandardExportsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite", database_url="",
            daily_stats_timezone="Europe/Moscow", track_deal_category_id="0",
            deal_refusal_reason_field="UF_REASON",
        )
        self.settings_patch = patch("app.standard_exports.get_settings", return_value=self.settings)
        self.store_patch = patch("app.store.get_settings", return_value=self.settings)
        self.settings_patch.start(); self.store_patch.start()
        store.execute(
            """INSERT INTO crm_item_events(entity_type,entity_id,event_type,old_value,new_value,occurred_at)
               VALUES (?,?,?,?,?,?)""",
            ("lead", "10", "assignee_changed", "26", "110", "2026-10-02T10:05:00+03:00"),
        )

    async def asyncTearDown(self):
        self.settings_patch.stop(); self.store_patch.stop(); self.tmp.cleanup()

    async def test_exact_columns_and_stage_change_history(self):
        data = await standard_exports.collect_standard_sales_export(
            date(2026, 10, 1), date(2026, 10, 31), client=FakeClient(),
        )
        self.assertEqual(data["leads"][0]["handoff_at"], "2026-10-02T10:05:00+03:00")
        self.assertEqual(data["deals"][0]["stage_changed_at"], "2026-10-04T10:55:00+03:00")
        self.assertEqual(data["deals"][0]["refusal_reason"], "Мусор")
        workbook = load_workbook(BytesIO(standard_exports.build_standard_sales_workbook(data)))
        self.assertEqual([cell.value for cell in workbook["Лиды"][1]], [
            "ID", "Дата создания", "Передан менеджеру", "Ответственный", "Стадия", "Источник", "Телефон",
        ])
        self.assertEqual([cell.value for cell in workbook["Сделки"][1]], [
            "ID", "Дата создания", "Ответственный", "Стадия", "Дата изменения стадии",
            "Источник", "Телефон контакта", "Сумма", "Валюта", "Причина отказа",
        ])
        self.assertEqual(workbook["Лиды"]["G2"].number_format, "@")
        self.assertEqual(workbook["Сделки"]["G2"].number_format, "@")


if __name__ == "__main__":
    unittest.main()
