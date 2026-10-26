import asyncio
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import call_tracking, store


class FakeTasksClient:
    def __init__(self, tasks=None, deals=None, contacts=None, lead_statuses=None, deal_stages=None):
        self.tasks = tasks or []
        self.deals = deals or []
        self.contacts = contacts or []
        self.lead_statuses = lead_statuses or [{"STATUS_ID": "NEW", "NAME": "Новый"}]
        self.deal_stages = deal_stages or [{"STATUS_ID": "NEW", "NAME": "Новая сделка"}]

    async def get_tasks_for_crm_items(self, bindings):
        return self.tasks

    async def get_lead_statuses(self):
        return self.lead_statuses

    async def get_deal_stages(self, category_id="0"):
        return self.deal_stages

    async def get_deals_by_ids(self, deal_ids):
        return self.deals

    async def get_contacts_by_ids(self, contact_ids):
        return self.contacts


class CallTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + "/db.sqlite", database_url="",
            daily_stats_timezone="Europe/Moscow",
            bitrix_webhook_url="https://example.bitrix24.ru/rest/1/token/",
            track_deal_category_id="0",
        )
        self.store_patch = patch("app.store.get_settings", return_value=self.settings)
        self.calls_patch = patch("app.call_tracking.get_settings", return_value=self.settings)
        self.store_patch.start(); self.calls_patch.start()

    def tearDown(self):
        self.calls_patch.stop(); self.store_patch.stop(); self.tmp.cleanup()

    def test_history_event_is_normalized_without_raw_secret(self):
        saved = call_tracking.save_history_event({
            "cmd": "history", "callid": "abc", "ext": "101", "type": "out",
            "phone": "+7 (999) 123-45-67", "start": "20261005T070500Z",
            "duration": "125.0", "wait": "9", "status": "Success", "link": "https://records.example/a.mp3",
            "crm_token": "must-not-be-stored",
        })
        self.assertTrue(saved)
        row = store.rows("SELECT * FROM megafon_calls")[0]
        self.assertEqual(row["client_phone"], "9991234567")
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["wait_seconds"], 9)
        self.assertEqual(row["processing_status"], "not_applicable")
        self.assertIsNone(row["recording_url"])
        self.assertNotIn("must-not-be-stored", str(row))

    def test_legacy_numeric_user_map_is_accepted(self):
        from app.config import Settings

        settings = Settings.model_construct(megafon_bitrix_user_map="{101: 42}")
        self.assertEqual(settings.megafon_bitrix_users, {"101": "42"})

    def test_today_payload_calculates_reaction_from_latest_handoff(self):
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,created_date,current_stage_id,current_assignee_id,current_assignee_name,
                first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
            ("lead", "42", "2026-10-05", "NEW", "7", "Анна",
             "2026-10-05T10:00:00+03:00", "2026-10-05T10:00:00+03:00"),
        )
        store.execute("INSERT INTO seen_leads(lead_id) VALUES (?)", ("42",))
        store.execute("INSERT INTO seen_lead_phones(lead_id,phone) VALUES (?,?)", ("42", "9991234567"))
        store.execute(
            """INSERT INTO crm_item_events(entity_type,entity_id,event_type,new_value,occurred_at)
               VALUES (?,?,?,?,?)""",
            ("lead", "42", "assignee_changed", "7", "2026-10-05T10:02:00+03:00"),
        )
        call_tracking.save_history_event({
            "cmd": "history", "callid": "abc", "ext": "101", "type": "out",
            "phone": "89991234567", "start": "20261005T071000Z", "duration": 180,
            "status": "Success", "link": "https://records.example/a.mp3",
        })
        payload = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=FakeTasksClient(),
        ))
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["reaction_seconds"], 480)
        self.assertEqual(payload["items"][0]["duration_seconds"], 180)
        self.assertEqual(payload["items"][0]["stage_name"], "Новый")

    def test_unmapped_extension_does_not_hide_phone_match(self):
        self.settings.megafon_bitrix_users = {"701": "99"}
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,created_date,current_assignee_id,current_assignee_name,
                first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            ("lead", "42", "2026-10-05", "110", "Яна",
             "2026-10-05T10:00:00+03:00", "2026-10-05T10:00:00+03:00"),
        )
        store.execute("INSERT INTO seen_leads(lead_id) VALUES (?)", ("42",))
        store.execute("INSERT INTO seen_lead_phones(lead_id,phone) VALUES (?,?)", ("42", "9991234567"))
        call_tracking.save_history_event({
            "cmd": "history", "callid": "abc", "ext": "706", "type": "out",
            "phone": "89991234567", "start": "20261005T071000Z", "duration": 180,
            "status": "Success", "link": "https://records.example/a.mp3",
        })

        payload = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=FakeTasksClient(),
        ))

        self.assertEqual(payload["items"][0]["call_count"], 1)
        self.assertEqual(payload["items"][0]["duration_seconds"], 180)

    def test_reaction_counts_only_time_between_ten_and_nineteen_moscow(self):
        zone = ZoneInfo("Europe/Moscow")
        self.assertEqual(call_tracking._working_seconds_between(
            datetime(2026, 10, 5, 9, 0, tzinfo=zone),
            datetime(2026, 10, 5, 10, 15, tzinfo=zone), zone,
        ), 15 * 60)
        self.assertEqual(call_tracking._working_seconds_between(
            datetime(2026, 10, 5, 18, 50, tzinfo=zone),
            datetime(2026, 10, 6, 10, 10, tzinfo=zone), zone,
        ), 20 * 60)

    def test_task_created_by_manager_after_call_is_returned(self):
        self.settings.megafon_bitrix_users = {}
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,created_date,current_assignee_id,current_assignee_name,
                first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            ("lead", "42", "2026-10-05", "110", "Яна",
             "2026-10-05T10:00:00+03:00", "2026-10-05T10:00:00+03:00"),
        )
        store.execute("INSERT INTO seen_leads(lead_id) VALUES (?)", ("42",))
        store.execute("INSERT INTO seen_lead_phones(lead_id,phone) VALUES (?,?)", ("42", "9991234567"))
        call_tracking.save_history_event({
            "cmd": "history", "callid": "abc", "ext": "706", "type": "out",
            "phone": "89991234567", "start": "20261005T071000Z", "duration": 60,
            "status": "Success", "link": "https://records.example/a.mp3",
        })
        client = FakeTasksClient([{
            "id": "123", "title": "Перезвонить клиенту",
            "createdDate": "2026-10-05T10:12:00+03:00",
            "deadline": "2026-10-06T12:00:00+03:00",
            "responsibleId": "110", "ufCrmTask": ["L_42"],
        }])

        payload = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=client,
        ))

        lead = payload["items"][0]
        self.assertEqual(lead["task_title"], "Перезвонить клиенту")
        self.assertEqual(lead["task_deadline"], "2026-10-06T12:00:00+03:00")
        self.assertEqual(lead["task_origin"], "manual")
        self.assertTrue(lead["task_url"].endswith("/tasks/task/view/123/"))

    def test_automatic_task_does_not_count_as_manager_follow_up(self):
        self.settings.megafon_bitrix_users = {}
        self.settings.bitrix_automation_user_id_set = {"24"}
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,created_date,current_assignee_id,current_assignee_name,
                first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            ("lead", "42", "2026-10-05", "110", "Яна",
             "2026-10-05T10:00:00+03:00", "2026-10-05T10:00:00+03:00"),
        )
        store.execute("INSERT INTO seen_leads(lead_id) VALUES (?)", ("42",))
        store.execute("INSERT INTO seen_lead_phones(lead_id,phone) VALUES (?,?)", ("42", "9991234567"))
        call_tracking.save_history_event({
            "cmd": "history", "callid": "abc", "ext": "706", "type": "out",
            "phone": "89991234567", "start": "20261005T071000Z", "duration": 60,
            "status": "Success",
        })
        client = FakeTasksClient([{
            "id": "124", "title": "Прозвонить клиента по сделке #42",
            "createdDate": "2026-10-05T10:12:00+03:00",
            "deadline": "2026-10-05T11:12:00+03:00", "responsibleId": "110",
            "createdBy": "24", "ufCrmTask": ["L_42"],
        }])

        payload = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=client,
        ))

        self.assertIsNone(payload["items"][0]["task_title"])

    def test_today_payload_includes_deal_and_matches_contact_phone(self):
        self.settings.megafon_bitrix_users = {}
        store.execute(
            """INSERT INTO crm_items
               (entity_type,entity_id,created_date,current_stage_id,current_assignee_id,
                current_assignee_name,first_seen_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("deal", "77", "2026-10-05", "NEW", "110", "Яна",
             "2026-10-05T10:00:00+03:00", "2026-10-05T10:00:00+03:00"),
        )
        call_tracking.save_history_event({
            "cmd": "history", "callid": "deal-call", "ext": "706", "type": "out",
            "phone": "89991234567", "start": "20261005T071000Z", "duration": 90,
            "status": "Success",
        })
        client = FakeTasksClient(
            deals=[{"ID": "77", "CONTACT_ID": "9"}],
            contacts=[{"ID": "9", "PHONE": [{"VALUE": "+7 999 123-45-67"}]}],
        )

        payload = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=client,
        ))

        deal = payload["items"][0]
        self.assertEqual(deal["entity_type"], "deal")
        self.assertEqual(deal["stage_name"], "Новая сделка")
        self.assertEqual(deal["call_count"], 1)
        self.assertEqual(deal["duration_seconds"], 90)

    def test_stage_path_excludes_unprocessed_and_rejected_leads_are_last(self):
        for values in (
            ("1", "JUNK", "Ранний мусор", "2026-10-05T10:00:00+03:00"),
            ("2", "QUALIFIED", "Обычный лид", "2026-10-05T11:00:00+03:00"),
        ):
            store.execute(
                """INSERT INTO crm_items
                   (entity_type,entity_id,created_date,current_stage_id,current_assignee_name,
                    first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                ("lead", values[0], "2026-10-05", values[1], values[2], values[3], values[3]),
            )
        for stage, moment in (
            ("NEW", "2026-10-05T10:00:00+03:00"),
            ("QUALIFIED", "2026-10-05T10:05:00+03:00"),
            ("JUNK", "2026-10-05T10:20:00+03:00"),
        ):
            store.execute(
                """INSERT INTO crm_item_events
                   (entity_type,entity_id,event_type,new_value,occurred_at) VALUES (?,?,?,?,?)""",
                ("lead", "1", "created" if stage == "NEW" else "stage_changed", stage, moment),
            )
        store.execute(
            """INSERT INTO crm_item_events
               (entity_type,entity_id,event_type,new_value,occurred_at) VALUES (?,?,?,?,?)""",
            ("lead", "2", "stage_changed", "QUALIFIED", "2026-10-05T11:05:00+03:00"),
        )
        client = FakeTasksClient(lead_statuses=[
            {"STATUS_ID": "NEW", "NAME": "Не обработан"},
            {"STATUS_ID": "QUALIFIED", "NAME": "Квалифицирован"},
            {"STATUS_ID": "JUNK", "NAME": "Мусор"},
        ])

        items = asyncio.run(call_tracking.today_payload(
            now=datetime(2026, 10, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            client=client,
        ))["items"]

        self.assertEqual([item["entity_id"] for item in items], ["2", "1"])
        self.assertEqual(items[0]["stage_path"], ["Квалифицирован"])
        self.assertEqual(items[1]["stage_path"], ["Квалифицирован", "Мусор"])
        self.assertTrue(items[1]["is_rejected"])


if __name__ == "__main__":
    unittest.main()
