import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from apps.bot.backend.config import Settings

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
            planner_web_supervisor_bindings={"1": 1073597301},
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

    def test_two_supervisors_keep_separate_audit_and_dialer_identities(self):
        with api.store.connection(True) as db:
            db.execute("INSERT INTO employees(name,telegram_id,role) VALUES('AndreyK',699296552,'supervisor')")
        self.settings.planner_web_supervisor_bindings = {'101': 1073597301, '202': 699296552}
        identities = []
        for account, telegram_id in ((101, 1073597301), (202, 699296552)):
            FakeAsyncClient.response = FakeResponse(200, {
                'id': account, 'full_name': f'Account {account}', 'role': 'supervisor', 'is_active': True,
            })
            headers = {'Authorization': f'Bearer supervisor-{account}'}
            user = self.client.get('/api/me', headers=headers).json()['user']
            self.assertEqual(user['telegram_id'], telegram_id)
            identities.append(user['id'])
            response = self.client.post('/api/employees', headers=headers, json={'name': f'Employee {account}'})
            self.assertEqual(response.status_code, 201)
            with api.store.connection() as db:
                audit = db.execute('SELECT actor_id FROM audit ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(audit['actor_id'], user['id'])
            with patch('apps.planner.backend.api.dialer.current_session', return_value=None) as current:
                self.assertEqual(self.client.get('/api/dialer/session', headers=headers).status_code, 200)
                current.assert_called_once_with(telegram_id)
        self.assertNotEqual(*identities)

    def test_unmapped_disabled_or_manager_identity_cannot_become_supervisor(self):
        FakeAsyncClient.response = FakeResponse(200, {
            'id': 101, 'full_name': 'Verified supervisor', 'role': 'supervisor', 'is_active': True,
        })
        for binding in ({}, {'101': 999999999}, {'101': 7939114214}):
            with self.subTest(binding=binding):
                self.settings.planner_web_supervisor_bindings = binding
                response = self.client.get('/api/me', headers={'Authorization': 'Bearer restricted'})
                self.assertEqual(response.status_code, 403)
        self.settings.planner_web_supervisor_bindings = {'101': 1073597301}
        with api.store.connection(True) as db:
            db.execute('UPDATE employees SET active=0 WHERE telegram_id=1073597301')
        self.assertEqual(self.client.get('/api/me', headers={'Authorization': 'Bearer restricted'}).status_code, 403)

    def test_cached_token_rechecks_current_binding(self):
        FakeAsyncClient.response = FakeResponse(200, {
            'id': 1, 'full_name': 'Admin', 'role': 'admin', 'is_active': True,
        })
        headers = {'Authorization': 'Bearer cached-admin'}
        self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 200)
        self.settings.planner_web_supervisor_bindings = {}
        self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 403)

    def test_binding_configuration_rejects_shared_or_invalid_identities(self):
        required = dict(
            bitrix_webhook_url='https://example.invalid/', bitrix_application_token='test',
            telegram_bot_token='test', telegram_chat_id='1', telegram_webhook_secret='test',
            director_users='1', sales_department_id='1',
        )
        for bindings in ({'1': 1073597301, '2': 1073597301}, {'01': 1073597301},
                         {'0': 1073597301}, {'1': 0}, {'1': 2**63}, {'1': True}, {'1': 1.5}):
            with self.subTest(bindings=bindings), self.assertRaises(ValidationError):
                Settings(_env_file=None, **required, planner_web_supervisor_bindings=bindings)
        settings = Settings(_env_file=None, **required, planner_web_supervisor_bindings={'101': 1073597301})
        self.assertEqual(settings.planner_web_supervisor_bindings, {'101': 1073597301})

    def test_malformed_account_id_returns_a_controlled_error(self):
        for value in (None, True, '1', 0, -1, 2**63):
            with self.subTest(value=value):
                api._web_identity_cache.clear()
                FakeAsyncClient.response = FakeResponse(200, {
                    'id': value, 'full_name': 'Admin', 'role': 'admin', 'is_active': True,
                })
                self.assertEqual(self.client.get('/api/me', headers={'Authorization': 'Bearer malformed'}).status_code, 502)

    def test_unmapped_manager_is_denied(self):
        FakeAsyncClient.response = FakeResponse(200, {
            "id": 15, "full_name": "Чужой менеджер", "role": "manager",
            "is_active": True, "bitrix_user_id": "99999",
        })
        response = self.client.get("/api/me", headers={"Authorization": "Bearer unknown-token"})
        self.assertEqual(response.status_code, 403)

    def test_identity_cache_expires_without_extending_on_hits(self):
        clock = [0.0]
        verified = FakeResponse(200, {'id': 9, 'full_name': 'Manager', 'role': 'manager',
                                     'is_active': True, 'bitrix_user_id': '110'})
        with patch.object(api, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(FakeAsyncClient, 'get', new=AsyncMock(return_value=verified)) as get:
            headers = {'Authorization': 'Bearer first'}
            self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 200)
            clock[0] = 14
            self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 200)
            self.assertEqual(get.await_count, 1)
            clock[0] = 15
            self.assertEqual(self.client.get('/api/me', headers=headers).status_code, 200)
            self.assertEqual(get.await_count, 2)
            clock[0] = 31
            self.assertEqual(self.client.get('/api/me', headers={'Authorization': 'Bearer second'}).status_code, 200)
            self.assertEqual(len(api._web_identity_cache), 1)

    def test_crm_routes_expire_and_remove_unused_cache_entries(self):
        FakeAsyncClient.response = FakeResponse(200, {'id': 9, 'full_name': 'Manager', 'role': 'manager',
                                                    'is_active': True, 'bitrix_user_id': '110'})
        cases = [('summary', '/api/crm/summary?day=2026-11-01', '/api/crm/summary?day=2026-11-02', api._crm_cache),
                 ('performance', '/api/crm/performance?start=2026-11-01&end=2026-11-01',
                  '/api/crm/performance?start=2026-11-02&end=2026-11-02', api._performance_cache)]
        for method, first, second, cache in cases:
            with self.subTest(method=method):
                clock = [0.0]
                cache.clear()
                api._web_identity_cache.clear()
                service = SimpleNamespace(**{method: AsyncMock(return_value={'available': True})})
                try:
                    with patch.object(api, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                         patch.object(api, 'PlannerCRM', return_value=service):
                        headers = {'Authorization': 'Bearer manager-cache'}
                        self.assertEqual(self.client.get(first, headers=headers).status_code, 200)
                        clock[0] = 59
                        self.assertEqual(self.client.get(first, headers=headers).status_code, 200)
                        self.assertEqual(getattr(service, method).await_count, 1)
                        clock[0] = 60
                        self.assertEqual(self.client.get(first, headers=headers).status_code, 200)
                        self.assertEqual(getattr(service, method).await_count, 2)
                        clock[0] = 121
                        self.assertEqual(self.client.get(second, headers=headers).status_code, 200)
                        self.assertEqual(len(cache), 1)
                finally:
                    cache.clear()

    def test_all_caches_are_bounded_and_refreshing_one_key_does_not_evict_another(self):
        for cache in (api._web_identity_cache, api._crm_cache, api._performance_cache):
            with self.subTest(cache=id(cache)), patch.object(api, 'CACHE_MAX_ENTRIES', 2):
                cache.clear()
                try:
                    api._cache_put(cache, 'first', {'id': 1}, 60)
                    api._cache_put(cache, 'second', {'id': 2}, 60)
                    api._cache_put(cache, 'first', {'id': 3}, 60)
                    self.assertEqual(set(cache), {'first', 'second'})
                    api._cache_put(cache, 'third', {'id': 4}, 60)
                    self.assertEqual(set(cache), {'first', 'third'})
                    self.assertEqual(api._cache_get(cache, 'first', 60), {'id': 3})
                finally:
                    cache.clear()

    def test_invalid_leadflow_token_is_denied(self):
        FakeAsyncClient.response = FakeResponse(401, {"detail": "invalid"})
        response = self.client.get("/api/me", headers={"Authorization": "Bearer invalid-token"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
