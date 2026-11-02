"""Separate service startup, route ownership and packaged frontend checks."""
import asyncio
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from apps.bot.backend import main as bot
from apps.bot.backend.config import Settings
from apps.planner.backend import api, main as planner


class ApplicationLayoutTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Settings(
            _env_file=None, bitrix_webhook_url='https://example.invalid/rest/1/synthetic/',
            bitrix_application_token='synthetic', telegram_bot_token='123:synthetic',
            telegram_chat_id='0', telegram_webhook_secret='synthetic',
            director_users='', sales_department_id='1', database_url='',
            database_path=str(Path(directory) / 'bot.sqlite3'),
            manager_planner_database_path=str(Path(directory) / 'planner.sqlite3'),
            legacy_distribution_queue_path='',
        )
        previous = api.store
        self.addCleanup(setattr, api, 'store', previous)
        for target in ('apps.bot.backend.main.get_settings', 'apps.bot.backend.store.get_settings',
                       'apps.planner.backend.api.get_settings', 'apps.planner.backend.main.get_settings'):
            self.enterContext(patch(target, return_value=self.settings))
        self.enterContext(patch('apps.bot.backend.main.set_chat_menu_button', new=AsyncMock()))
        self.enterContext(patch('apps.bot.backend.main.close_http_client', new=AsyncMock()))
        self.enterContext(patch('apps.planner.backend.main.close_http_client', new=AsyncMock()))

    def test_planner_html_and_assets_keep_existing_urls(self):
        with TestClient(planner.app, raise_server_exceptions=True) as client:
            response = client.get('/manager/')
            self.assertEqual(response.status_code, 200)
            assets = re.findall(r'(?:src|href)="(/manager/assets/[^\"]+)"', response.text)
            self.assertTrue(any(asset.endswith('.js') for asset in assets))
            self.assertTrue(any(asset.endswith('.css') for asset in assets))
            for asset in assets:
                resource = client.get(asset)
                self.assertEqual(resource.status_code, 200)
                self.assertNotIn('text/html', resource.headers['content-type'])
            for route in ('/telegram/webhook', '/bitrix/webhook', '/megafon/webhook'):
                self.assertEqual(client.post(route).status_code, 404)

    def test_bot_does_not_initialize_planner_or_mount_its_routes(self):
        async def idle(*args):
            await asyncio.Event().wait()
        with ExitStack() as patches:
            for target in ('deals.polling_loop', 'manual.recovery_loop', 'reports.daily_report_loop',
                           'reconcile.reconciliation_loop', 'outcomes.daily_sync_loop',
                           '_bitrix_event_recovery_loop', 'distribution_flow.reminder_loop'):
                patches.enter_context(patch('apps.bot.backend.main.' + target, new=idle))
            initialize = patches.enter_context(patch('apps.planner.backend.api.initialize'))
            with TestClient(bot.app) as client:
                initialize.assert_not_called()
                self.assertFalse(Path(self.settings.manager_planner_database_path).exists())
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/manager/').status_code, 404)

    def test_planner_starts_without_starting_bot_workers(self):
        async def idle(*args):
            await asyncio.Event().wait()
        with ExitStack() as patches:
            workers = [patches.enter_context(patch('apps.bot.backend.main.' + target))
                       for target in ('deals.polling_loop', 'manual.recovery_loop', 'reports.daily_report_loop',
                                      'reconcile.reconciliation_loop', 'outcomes.daily_sync_loop',
                                      '_bitrix_event_recovery_loop', 'distribution_flow.reminder_loop')]
            outbox = patches.enter_context(patch('apps.planner.backend.main.sync.outbox_sync_loop', side_effect=idle))
            with TestClient(planner.app) as client:
                self.assertEqual(api.store.path, Path(self.settings.manager_planner_database_path))
                self.assertTrue(api.store.path.exists())
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/manager/healthz').status_code, 200)
                outbox.assert_called_once()
                for worker in workers:
                    worker.assert_not_called()
