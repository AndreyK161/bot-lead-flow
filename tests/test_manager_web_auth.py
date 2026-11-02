import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.planner.backend import api
from apps.planner.backend.auth import issue_web_session
from apps.planner.backend.store import Store


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeAsyncClient:
    response = FakeResponse(401, {})

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers):
        return self.response


class ManagerWebAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_store = api.store
        api.store = Store(Path(self.tmp.name) / "planner.sqlite3")
        api.store.initialize()
        api._web_identity_cache.clear()
        self.settings = SimpleNamespace(
            telegram_bot_token="telegram-secret",
            leadflow_web_auth_url="http://leadflow.test/api/v1/auth/me",
            leadflow_web_auth_timeout_seconds=1,
        )
        self.settings_patch = patch("apps.planner.backend.api.get_settings", return_value=self.settings)
        self.http_patch = patch("apps.planner.backend.api.httpx.AsyncClient", FakeAsyncClient)
        self.settings_patch.start(); self.http_patch.start()
        self.client = TestClient(api.create_app())

    def tearDown(self):
        self.client.close()
        self.http_patch.stop(); self.settings_patch.stop()
        api.store = self.previous_store
        api._web_identity_cache.clear()
        self.tmp.cleanup()

    def test_manager_is_mapped_by_verified_bitrix_id(self):
        FakeAsyncClient.response = FakeResponse(200, {
            "id": 9, "full_name": "Яна из Lead Flow", "role": "manager",
            "is_active": True, "bitrix_user_id": "110",
        })
        response = self.client.get("/api/me", headers={"Authorization": "Bearer manager-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["bitrix_id"], 110)
        self.assertEqual(response.json()["user"]["name"], "Яна из Lead Flow")
        self.assertEqual(response.json()["user"]["role"], "manager")

    def test_explicit_bearer_uses_web_identity_despite_telegram_cookie(self):
        cookies = {
            "invalid": "corrupt-session",
            "expired": issue_web_session(1073597301, self.settings.telegram_bot_token, now=1, ttl=1),
            "another_user": issue_web_session(1073597301, self.settings.telegram_bot_token),
        }
        FakeAsyncClient.response = FakeResponse(200, {
            "id": 9, "full_name": "Web manager", "role": "manager",
            "is_active": True, "bitrix_user_id": "110",
        })
        with patch("apps.planner.backend.api.access_registry.authorized_identity", return_value={
            "role": "supervisor", "bitrix_user_id": None,
        }):
            for case, cookie in cookies.items():
                with self.subTest(cookie=case):
                    api._web_identity_cache.clear()
                    response = self.client.get("/api/me", headers={
                        "Authorization": "Bearer valid-web-token",
                        "Cookie": f"{api.TELEGRAM_SESSION_COOKIE}={cookie}",
                    })
                    self.assertEqual(response.status_code, 200)
                    user = response.json()["user"]
                    self.assertEqual(user["role"], "manager")
                    self.assertEqual(user["bitrix_id"], 110)
                    self.assertEqual(user["web_user_id"], 9)

    def test_rejected_authorization_does_not_fall_back_to_valid_cookie(self):
        cookie = issue_web_session(1073597301, self.settings.telegram_bot_token)
        FakeAsyncClient.response = FakeResponse(401, {"detail": "invalid"})
        with patch("apps.planner.backend.api.access_registry.authorized_identity", return_value={
            "role": "supervisor", "bitrix_user_id": None,
        }):
            for authorization in ("Bearer invalid-token", "Bearer ", "Basic unsupported"):
                with self.subTest(authorization=authorization):
                    response = self.client.get("/api/me", headers={
                        "Authorization": authorization,
                        "Cookie": f"{api.TELEGRAM_SESSION_COOKIE}={cookie}",
                    })
                    self.assertEqual(response.status_code, 401)

    def test_admin_uses_supervisor_scope_without_client_role_override(self):
        FakeAsyncClient.response = FakeResponse(200, {
            "id": 1, "full_name": "Администратор", "role": "admin",
            "is_active": True, "bitrix_user_id": None,
        })
        response = self.client.get("/api/me", headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "supervisor")
        self.assertEqual(response.json()["user"]["name"], "Администратор")

    def test_unmapped_manager_is_denied(self):
        FakeAsyncClient.response = FakeResponse(200, {
            "id": 15, "full_name": "Чужой менеджер", "role": "manager",
            "is_active": True, "bitrix_user_id": "99999",
        })
        response = self.client.get("/api/me", headers={"Authorization": "Bearer unknown-token"})
        self.assertEqual(response.status_code, 403)

    def test_invalid_leadflow_token_is_denied(self):
        FakeAsyncClient.response = FakeResponse(401, {"detail": "invalid"})
        response = self.client.get("/api/me", headers={"Authorization": "Bearer invalid-token"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
