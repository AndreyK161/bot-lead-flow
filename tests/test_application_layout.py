"""The shared ASGI entry point serves the relocated Planner frontend."""
import asyncio
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from apps.bot.backend import main
from apps.bot.backend.config import Settings
from apps.bot.backend.main import app
from apps.planner.backend import api as planner_api


class ApplicationLayoutTests(unittest.TestCase):
    def test_planner_html_and_its_assets_are_served_at_existing_urls(self):
        # No lifespan: this checks routing and packaged files without starting
        # background jobs, connecting to a database, or contacting Telegram.
        client = TestClient(app)
        try:
            response = client.get('/manager/')
            self.assertEqual(response.status_code, 200)
            assets = re.findall(r'(?:src|href)="(/manager/assets/[^\"]+)"', response.text)
            self.assertTrue(any(asset.endswith('.js') for asset in assets))
            self.assertTrue(any(asset.endswith('.css') for asset in assets))
            for asset in assets:
                with self.subTest(asset=asset):
                    resource = client.get(asset)
                    self.assertEqual(resource.status_code, 200)
                    self.assertTrue(resource.content)
                    self.assertNotIn('text/html', resource.headers['content-type'])
        finally:
            client.close()

    def test_shared_lifespan_initializes_both_databases_and_serves_planner(self):
        async def idle(*args):
            await asyncio.Event().wait()

        previous_store = planner_api.store
        self.addCleanup(setattr, planner_api, 'store', previous_store)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            settings = Settings(
                _env_file=None,
                bitrix_webhook_url='https://example.invalid/rest/1/synthetic/',
                bitrix_application_token='synthetic', telegram_bot_token='123:synthetic',
                telegram_chat_id='0', telegram_webhook_secret='synthetic',
                director_users='', sales_department_id='1', database_url='',
                database_path=str(Path(directory) / 'bot.sqlite3'),
                manager_planner_database_path=str(Path(directory) / 'planner.sqlite3'),
                legacy_distribution_queue_path='',
            )
            for target in (
                'apps.bot.backend.main.get_settings',
                'apps.bot.backend.store.get_settings',
                'apps.planner.backend.api.get_settings',
            ):
                patches.enter_context(patch(target, return_value=settings))
            for target in (
                'apps.bot.backend.main.deals.polling_loop',
                'apps.bot.backend.main.manual.recovery_loop',
                'apps.bot.backend.main.reports.daily_report_loop',
                'apps.bot.backend.main.reconcile.reconciliation_loop',
                'apps.bot.backend.main.outcomes.daily_sync_loop',
                'apps.bot.backend.main._bitrix_event_recovery_loop',
                'apps.bot.backend.main.distribution_flow.reminder_loop',
            ):
                patches.enter_context(patch(target, new=idle))
            patches.enter_context(patch('apps.bot.backend.main.set_chat_menu_button', new=AsyncMock()))
            patches.enter_context(patch('apps.bot.backend.main.close_http_client', new=AsyncMock()))
            with TestClient(main.app) as client:
                self.assertTrue(Path(settings.database_path).is_file())
                self.assertEqual(planner_api.store.path, Path(settings.manager_planner_database_path))
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/manager/healthz').status_code, 200)
                self.assertEqual(client.get('/manager/').status_code, 200)
