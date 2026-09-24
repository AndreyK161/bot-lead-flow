import hashlib
import hmac
import json
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

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


class MiniAppReportTests(unittest.TestCase):
    def test_groups_sources_and_exposes_cohort_totals(self):
        payload = miniapp.report_payload({
            "start": date(2026, 9, 1),
            "end": date(2026, 9, 23),
            "checked_at": datetime(2026, 9, 24, 12, 0),
            "portal_domain": "example.bitrix24.ru",
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
        self.assertEqual(payload["managers"], [
            {"id": "1", "name": "Анна"}, {"id": "2", "name": "Борис"},
        ])

    def test_page_has_native_date_inputs_and_both_tabs(self):
        html = Path(miniapp.HTML_PATH).read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count('type="date"'), 2)
        self.assertIn('data-tab="leads"', html)
        self.assertIn('data-tab="deals"', html)
        self.assertIn("Показать отчёт", html)
        self.assertIn('id="manager-options"', html)
        self.assertIn('className = \'entity-link\'', html)


if __name__ == "__main__":
    unittest.main()
