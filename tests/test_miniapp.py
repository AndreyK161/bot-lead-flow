import hashlib
import hmac
import json
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import HTTPException

from app import miniapp


BOT_TOKEN = "123456:test-token"
TEST_USER_ID = 1297686797


def signed_init_data(*, user_id=TEST_USER_ID, auth_date=None, token=BOT_TOKEN):
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MiniAppAuthenticationTests(unittest.TestCase):
    def test_accepts_valid_telegram_init_data_for_test_user(self):
        user = miniapp.validate_init_data(signed_init_data(), BOT_TOKEN)
        self.assertEqual(user["id"], TEST_USER_ID)

    def test_rejects_modified_signature(self):
        payload = signed_init_data().replace("Test", "Other")
        with self.assertRaisesRegex(ValueError, "invalid hash"):
            miniapp.validate_init_data(payload, BOT_TOKEN)

    def test_rejects_expired_init_data(self):
        now = 2_000_000_000
        payload = signed_init_data(auth_date=now - miniapp.MAX_AUTH_AGE_SECONDS - 1)
        with self.assertRaisesRegex(ValueError, "expired"):
            miniapp.validate_init_data(payload, BOT_TOKEN, now=now)

    def test_authorizes_admin_without_director_role(self):
        request = SimpleNamespace(headers={"X-Telegram-Init-Data": signed_init_data()})
        settings = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            director_user_id_set=set(),
            admin_user_id_set={TEST_USER_ID},
        )
        with patch("app.miniapp.get_settings", return_value=settings):
            self.assertEqual(miniapp._authorized_user(request), TEST_USER_ID)

    def test_rejects_regular_manager(self):
        request = SimpleNamespace(headers={"X-Telegram-Init-Data": signed_init_data()})
        settings = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            director_user_id_set=set(),
            admin_user_id_set=set(),
        )
        with (
            patch("app.miniapp.get_settings", return_value=settings),
            patch("app.miniapp.planner_access.enabled_user_ids", return_value=set()),
        ):
            with self.assertRaises(HTTPException) as ctx:
                miniapp._authorized_user(request)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_authorizes_only_enabled_planner_supervisor_for_reports(self):
        supervisor_id = 1073597301
        request = SimpleNamespace(headers={
            "X-Telegram-Init-Data": signed_init_data(user_id=supervisor_id),
        })
        settings = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            director_user_id_set=set(), admin_user_id_set=set(),
            super_admin_user_id_set=set(),
        )
        with (
            patch("app.miniapp.get_settings", return_value=settings),
            patch(
                "app.miniapp.planner_access.enabled_user_ids",
                return_value={supervisor_id},
            ),
        ):
            self.assertEqual(miniapp._authorized_user(request), supervisor_id)

    def test_authorizes_director_for_calls_section(self):
        request = SimpleNamespace(headers={"X-Telegram-Init-Data": signed_init_data()})
        settings = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            director_user_id_set={TEST_USER_ID}, admin_user_id_set=set(),
            super_admin_user_id_set=set(),
        )
        with patch("app.miniapp.get_settings", return_value=settings):
            self.assertEqual(miniapp._authorized_user(request), TEST_USER_ID)

    def test_authorizes_super_admin_for_calls_section(self):
        request = SimpleNamespace(headers={"X-Telegram-Init-Data": signed_init_data()})
        settings = SimpleNamespace(
            telegram_bot_token=BOT_TOKEN,
            director_user_id_set=set(), admin_user_id_set=set(),
            super_admin_user_id_set={TEST_USER_ID},
        )
        with patch("app.miniapp.get_settings", return_value=settings):
            self.assertEqual(miniapp._authorized_user(request), TEST_USER_ID)


class MiniAppReportTests(unittest.TestCase):
    def test_accepts_full_calendar_year(self):
        miniapp._validate_period(date(2026, 1, 1), date(2026, 12, 31))

    def test_groups_sources_and_exposes_cohort_totals(self):
        payload = miniapp.report_payload({
            "start": date(2026, 9, 1),
            "end": date(2026, 9, 23),
            "checked_at": datetime(2026, 9, 24, 12, 0),
            "portal_domain": "example.bitrix24.ru",
            "managers": [
                {"id": "1", "name": "Анна"}, {"id": "2", "name": "Борис"},
                {"id": "3", "name": "Вера"}, {"id": "4", "name": "Глеб"},
            ],
            "unique_total": 3,
            "direct_deal_count": 1,
            "leads": [
                {
                    "lead_id": "10", "deal_id": "", "source_name": "Сайт",
                    "result_stage": "Недозвон", "converted": False, "contract": False,
                    "manager_id": "1", "manager_name": "Анна",
                },
                {
                    "lead_id": "11", "deal_id": "20", "source_name": "Сайт",
                    "result_stage": "Сконвертирован", "converted": True, "contract": True,
                    "manager_id": "2", "manager_name": "Борис",
                },
            ],
            "deals": [
                {
                    "deal_id": "20", "source_name": "Сайт", "stage": "Приоритет",
                    "contract": True, "converted_from_report_lead": True,
                    "manager_id": "2", "manager_name": "Борис",
                },
                {
                    "deal_id": "21", "source_name": "Telegram", "stage": "Не обработан",
                    "contract": False, "converted_from_report_lead": False,
                    "manager_id": "1", "manager_name": "Анна",
                },
            ],
        })

        self.assertEqual(payload["totals"], {
            "unique": 3, "leads": 2, "direct_deals": 1,
            "converted": 1, "deals": 2, "contracts": 1,
        })
        self.assertEqual(payload["leads"][0]["source"], "Сайт")
        self.assertEqual(payload["leads"][0]["total"], 2)
        site_deals = next(row for row in payload["deals"] if row["source"] == "Сайт")
        self.assertEqual(site_deals["contracts"], 1)
        item = site_deals["stages"][0]["items"][0]
        self.assertEqual(item["url"], "https://example.bitrix24.ru/crm/deal/details/20/")
        self.assertEqual(item["manager_name"], "Борис")
        self.assertEqual(len(payload["managers"]), 4)
        self.assertEqual(payload["managers"][-1], {"id": "4", "name": "Глеб"})

    def test_page_has_native_date_inputs_and_both_tabs(self):
        html = Path(miniapp.HTML_PATH).read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count('type="date"'), 2)
        self.assertIn('data-tab="leads"', html)
        self.assertIn('data-tab="deals"', html)
        self.assertIn("Показать отчёт", html)
        self.assertIn('id="manager-options"', html)
        self.assertIn('className = \'entity-link\'', html)
        self.assertIn('className = \'progress-fill\'', html)
        self.assertIn('id="calls-nav" class="hidden"', html)
        self.assertIn('id="clients-nav"', html)
        self.assertIn("/api/miniapp/clients.xlsx", html)
        self.assertIn('id="clients-search"', html)
        self.assertIn('id="worktime-nav"', html)
        self.assertIn("/api/miniapp/worktime.xlsx", html)
        self.assertIn('id="tasks-nav"', html)
        self.assertIn("/api/miniapp/tasks.xlsx", html)
        self.assertIn("/api/miniapp/sales.xlsx", html)
        self.assertIn("/api/miniapp/telephony.xlsx", html)
        self.assertIn('id="presence-list"', html)
        self.assertIn("/api/miniapp/managers/presence", html)
        self.assertIn("window.setInterval(loadPresence, 60000)", html)

    def test_presence_payload_normalizes_statuses(self):
        with patch("app.miniapp._manager_bitrix_map", return_value={"анна": 42}):
            payload = miniapp._presence_payload([
                {
                    "id": 1, "full_name": "Анна", "presence_status": "working",
                    "presence_checked_at": "2026-10-16T10:00:00+00:00",
                },
                {"id": 2, "full_name": "Борис", "presence_status": "unexpected"},
            ])
        self.assertEqual(payload["managers"][0]["status"], "working")
        self.assertEqual(payload["managers"][0]["bitrix_id"], 42)
        self.assertEqual(payload["managers"][1]["status"], "unknown")
        self.assertEqual(payload["checked_at"], "2026-10-16T10:00:00+00:00")

    def test_manager_name_key_is_case_and_punctuation_insensitive(self):
        self.assertEqual(
            miniapp._manager_name_key("Евгений Гринёв"),
            miniapp._manager_name_key("евгений-гринев"),
        )

    def test_manager_bitrix_map_uses_access_registry_and_links(self):
        with (
            patch("app.miniapp.store.list_planner_access", return_value=[{
                "display_name": "Евгений Гринёв", "bitrix_user_id": "50",
                "role": "manager", "active": 1,
            }]),
            patch("app.miniapp.store.list_links", return_value=[{
                "bitrix_name": "Яна Азизова", "bitrix_user_id": "110",
            }]),
            patch("app.miniapp.store.rows", return_value=[]),
        ):
            result = miniapp._manager_bitrix_map()
        self.assertEqual(result["евгенийгринев"], 50)
        self.assertEqual(result["янаазизова"], 110)


class MiniAppReportJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        miniapp._jobs.clear()

    async def asyncTearDown(self):
        for job in miniapp._jobs.values():
            task = job.get("task")
            if task and not task.done():
                task.cancel()
        miniapp._jobs.clear()

    async def test_background_job_reports_progress_and_result(self):
        expected = {"totals": {"unique": 42}}

        async def build(_start, _end, progress):
            progress(65, "Загружены сделки")
            return expected

        with (
            patch("app.miniapp._authorized_user", return_value=TEST_USER_ID),
            patch("app.miniapp._cached_report", side_effect=build),
        ):
            started = await miniapp.start_mini_app_report_job(
                None, date(2026, 1, 1), date(2026, 12, 31),
            )
            job = miniapp._jobs[started["job_id"]]
            await job["task"]
            response = await miniapp.mini_app_report_job(None, started["job_id"])

        self.assertEqual(response["status"], "done")
        self.assertEqual(response["progress"], 100)
        self.assertEqual(response["result"], expected)


class MiniAppManagerStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_leads_and_calls_for_selected_manager(self):
        crm = SimpleNamespace(
            call_statistics=lambda *_: _async_value({
                "configured": True, "total": 7, "outbound": 6, "answered": 5,
                "long_calls": 2, "duration_seconds": 900,
            }),
            processed_leads=lambda *_: _async_value({"processed": 4, "stages": {"Квалифицирован": 4}}),
        )
        presence = {
            "managers": [{"bitrix_id": 50, "name": "Евгений Гринёв", "status": "working"}],
        }
        miniapp._manager_stats_cache.clear()
        with (
            patch("app.miniapp._authorized_user", return_value=TEST_USER_ID),
            patch("app.miniapp._allowed_manager_ids", return_value={50}),
            patch("app.miniapp._manager_presence_payload", return_value=presence),
            patch("app.miniapp.PlannerCRM", return_value=crm),
        ):
            result = await miniapp.mini_app_manager_stats(None, 50, date(2026, 10, 28))
        self.assertEqual(result["manager"]["name"], "Евгений Гринёв")
        self.assertEqual(result["leads"]["processed"], 4)
        self.assertEqual(result["calls"]["duration_seconds"], 900)

    async def test_rejects_manager_outside_sales_registry(self):
        with (
            patch("app.miniapp._authorized_user", return_value=TEST_USER_ID),
            patch("app.miniapp._allowed_manager_ids", return_value={50}),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await miniapp.mini_app_manager_stats(None, 999, date(2026, 10, 28))
        self.assertEqual(ctx.exception.status_code, 404)


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
