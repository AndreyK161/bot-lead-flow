import unittest
from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from app.worktime import build_worktime_payload, build_worktime_workbook


class WorktimeTests(unittest.TestCase):
    def test_builds_shift_and_all_breaks(self):
        raw = {"managers": [{
            "id": 1,
            "full_name": "Анна Менеджер",
            "presence_status": "offline",
            "events": [
                {"event_type": "workday_started", "occurred_at": "2026-10-16T10:00:00+03:00"},
                {"event_type": "lunch_started", "occurred_at": "2026-10-16T12:00:00+03:00"},
                {"event_type": "lunch_ended", "occurred_at": "2026-10-16T12:30:00+03:00"},
                {"event_type": "lunch_started", "occurred_at": "2026-10-16T14:00:00+03:00"},
                {"event_type": "lunch_ended", "occurred_at": "2026-10-16T14:10:00+03:00"},
                {"event_type": "workday_ended", "occurred_at": "2026-10-16T19:00:00+03:00"},
            ],
        }]}
        data = build_worktime_payload(
            raw, date(2026, 10, 16), date(2026, 10, 16),
            now=datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        row = data["items"][0]
        self.assertEqual(len(row["breaks"]), 2)
        self.assertEqual(row["break_seconds"], 40 * 60)
        self.assertEqual(row["work_seconds"], 8 * 3600 + 20 * 60)
        self.assertTrue(row["start_at"].endswith("10:00:00+03:00"))
        self.assertTrue(row["end_at"].endswith("19:00:00+03:00"))

        sheet = load_workbook(BytesIO(build_worktime_workbook(data)))["Табель"]
        self.assertEqual([cell.value for cell in sheet[1]], [
            "Дата", "Менеджер", "Начало дня", "Конец дня", "Перерывы",
            "Всего перерывов", "Рабочее время",
        ])
        self.assertEqual(sheet["E2"].value, "12:00–12:30; 14:00–14:10")
        self.assertEqual(sheet["F2"].value, "00:40")
        self.assertEqual(sheet["G2"].value, "08:20")

    def test_shows_current_managers_even_without_events(self):
        raw = {"managers": [{
            "id": 2, "full_name": "Борис", "presence_status": "offline", "events": [],
        }]}
        current = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        data = build_worktime_payload(
            raw, date(2026, 10, 19), date(2026, 10, 19), now=current,
        )
        self.assertEqual(len(data["items"]), 1)
        self.assertIsNone(data["items"][0]["start_at"])


if __name__ == "__main__":
    unittest.main()
