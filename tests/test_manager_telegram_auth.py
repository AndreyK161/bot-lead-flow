import hashlib
import hmac
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.manager_planner import api
from app.manager_planner.auth import (
    InvalidTelegramLogin,
    issue_web_session,
    validate_login_widget,
    validate_web_session,
)
from app.manager_planner.store import Store


BOT_TOKEN = "123456:test-token"
NOW = 1_800_000_000


def signed_login(user_id=1297686797, auth_date=NOW):
    payload = {
        "id": user_id,
        "first_name": "Андрей",
        "username": "test_user",
        "auth_date": auth_date,
    }
    check = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return payload


class ManagerTelegramAuthTests(unittest.TestCase):
    def test_accepts_signed_login_widget_payload(self):
        self.assertEqual(validate_login_widget(signed_login(), BOT_TOKEN, now=NOW), 1297686797)

    def test_rejects_modified_or_expired_widget_payload(self):
        modified = signed_login()
        modified["first_name"] = "Другой"
        with self.assertRaises(InvalidTelegramLogin):
            validate_login_widget(modified, BOT_TOKEN, now=NOW)
        with self.assertRaises(InvalidTelegramLogin):
            validate_login_widget(signed_login(auth_date=NOW - 601), BOT_TOKEN, now=NOW)

    def test_session_round_trip_and_expiry(self):
        session = issue_web_session(1297686797, BOT_TOKEN, ttl=3600, now=NOW)
        self.assertEqual(validate_web_session(session, BOT_TOKEN, now=NOW + 3599), 1297686797)
        with self.assertRaises(InvalidTelegramLogin):
            validate_web_session(session, BOT_TOKEN, now=NOW + 3601)

    def test_rejects_tampered_session(self):
        session = issue_web_session(1297686797, BOT_TOKEN, now=NOW)
        with self.assertRaises(InvalidTelegramLogin):
            validate_web_session(session[:-1] + ("0" if session[-1] != "0" else "1"), BOT_TOKEN, now=NOW)

    def test_login_endpoint_issues_session_for_enabled_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_store = api.store
            api.store = Store(Path(directory) / "planner.sqlite3")
            api.store.initialize()
            settings = SimpleNamespace(
                telegram_bot_token=BOT_TOKEN,
                telegram_bot_username="lead_notification_prav_buro_bot",
                telegram_login_max_age_seconds=600,
                telegram_web_session_seconds=43200,
                leadflow_web_auth_url="",
                leadflow_web_auth_timeout_seconds=1,
            )
            identity = {
                "telegram_id": 1297686797, "role": "owner",
                "bitrix_user_id": None, "active": True,
            }
            try:
                with patch("app.manager_planner.api.get_settings", return_value=settings), patch(
                    "app.manager_planner.api.access_registry.authorized_identity", return_value=identity,
                ):
                    with TestClient(api.create_app()) as client:
                        response = client.post(
                            "/api/auth/telegram",
                            json=signed_login(auth_date=int(time.time())),
                        )
                        self.assertEqual(response.status_code, 200)
                        session = response.json()["access_token"]
                        me = client.get("/api/me", headers={"Authorization": f"tgweb {session}"})
                        self.assertEqual(me.status_code, 200)
                        self.assertEqual(me.json()["user"]["telegram_id"], 1297686797)
            finally:
                api.store = previous_store


if __name__ == "__main__":
    unittest.main()
