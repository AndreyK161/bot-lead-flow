import tempfile
import unittest
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from app import store, task_reports


class FakeClient:
    async def get_department_users(self, department_id):
        return [
            {"ID": "110", "NAME": "Яна", "LAST_NAME": "Азизова"},
            {"ID": "26", "NAME": "Анастасия", "LAST_NAME": "Коравчук"},
        ]

    async def get_tasks_by_deadline(self, start_iso, end_iso, responsible_ids):
        assert responsible_ids == ["110"]
        return [
            {
                "id": "1", "title": "CRM: Позвонить", "responsibleId": "110",
                "createdBy": "110", "creator": {"name": "Яна Азизова"},
                "createdDate": "2026-10-19T10:00:00+03:00",
                "deadline": "2026-10-20T12:30:00+03:00", "status": "2",
                "ufCrmTask": ["D_42"],
            },
            {
                "id": "2", "title": "Прозвонить клиента по сделке #42",
                "responsibleId": "110", "createdBy": "24",
                "creator": {"name": "Никита"},
                "createdDate": "2026-10-19T10:01:00+03:00",
                "deadline": "2026-10-20T13:00:00+03:00", "closedDate": "2026-10-20T12:55:00+03:00",
                "status": "5", "ufCrmTask": ["CRM_DEAL_42", "D_42"],
            },
        ]

    async def get_leads_by_ids(self, ids):
        return []

    async def get_deals_by_ids(self, ids):
        return [{"ID": "42", "CONTACT_ID": "9"}]

    async def get_contacts_full_by_ids(self, ids):
        return [{"ID": "9", "PHONE": [{"VALUE": "+7 999 000-11-22"}]}]


class TaskReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite", database_url="",
            daily_stats_timezone="Europe/Moscow", sales_department_id="5",
            bitrix_automation_user_id_set={"24"}, megafon_bitrix_users={},
            bitrix_webhook_url="https://example.bitrix24.ru/rest/1/token/",
        )
        self.settings_patch = patch("app.task_reports.get_settings", return_value=self.settings)
        self.store_patch = patch("app.store.get_settings", return_value=self.settings)
        self.settings_patch.start(); self.store_patch.start()

    async def asyncTearDown(self):
        self.settings_patch.stop(); self.store_patch.stop(); self.tmp.cleanup()

    async def test_separates_manual_and_automatic_tasks(self):
        data = await task_reports.collect_task_report(
            date(2026, 10, 1), date(2026, 10, 31), client=FakeClient(),
        )
        self.assertEqual(data["totals"]["all"], 2)
        self.assertEqual(data["totals"]["manual"], 1)
        self.assertEqual(data["totals"]["automatic"], 1)
        self.assertEqual(data["items"][0]["type"], "Звонок")
        self.assertEqual(data["items"][0]["origin"], "manual")
        self.assertEqual(data["items"][0]["deal_id"], "42")
        self.assertEqual(data["items"][0]["phone"], "+7 999 000-11-22")
        self.assertEqual(data["items"][1]["callback_status"], "Не применяется")

        sheet = load_workbook(BytesIO(task_reports.build_task_workbook(data)))["Дела и задачи"]
        self.assertEqual(sheet["E2"].value, "Человек")
        self.assertEqual(sheet["E3"].value, "Робот")
        self.assertEqual(sheet["J2"].number_format, "@")
        self.assertIsNotNone(sheet["L2"].hyperlink)


if __name__ == "__main__":
    unittest.main()
