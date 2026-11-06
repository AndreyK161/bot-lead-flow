import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch
from types import SimpleNamespace

from apps.bot.backend.bitrix_client import BitrixClient, BitrixApiError


class JunkStageTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status='JUNK', reason='2110'):
        client = BitrixClient(webhook_url='https://example.com/rest/1/test/')
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'JUNK', 'NAME': 'Некачественный лид', 'SEMANTICS': 'F'},
        ])
        client._call_all = AsyncMock(return_value=[{
            'FIELD_NAME': 'UF_REASON',
            'LIST': [
                {'ID': '2110', 'VALUE': 'Мусор'},
                {'ID': '2118', 'VALUE': 'Дубль'},
            ],
        }])
        client.update_lead = AsyncMock()
        client.get_lead = AsyncMock(return_value={
            'ID': '42', 'STATUS_ID': status, 'UF_REASON': reason,
        })
        return client

    @staticmethod
    def settings():
        return SimpleNamespace(
            lead_failed_status_id='JUNK',
            lead_failure_reason_field='UF_REASON',
            lead_junk_reason_name='Мусор',
        )

    async def test_uses_common_failed_stage_with_junk_reason(self):
        client = self.client()
        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=self.settings()):
            lead = await client.move_to_junk('42')
        client.update_lead.assert_awaited_once_with('42', {
            'STATUS_ID': 'JUNK', 'UF_REASON': '2110',
        })
        self.assertEqual(lead['UF_REASON'], '2110')

    async def test_rejects_silent_failure_to_change_stage(self):
        client = self.client(status='NEW')
        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=self.settings()):
            with self.assertRaisesRegex(BitrixApiError, 'не подтвердил'):
                await client.move_to_junk('42')

    async def test_missing_failed_stage_does_not_modify_lead(self):
        client = self.client()
        client.get_lead_statuses.return_value = []
        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=self.settings()):
            with self.assertRaises(BitrixApiError):
                await client.move_to_junk('42')
        client.update_lead.assert_not_awaited()

    async def test_missing_junk_reason_does_not_modify_lead(self):
        client = self.client()
        client._call_all.return_value = [{
            'FIELD_NAME': 'UF_REASON', 'LIST': [{'ID': '2118', 'VALUE': 'Дубль'}],
        }]
        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=self.settings()):
            with self.assertRaisesRegex(BitrixApiError, 'Мусор'):
                await client.move_to_junk('42')
        client.update_lead.assert_not_awaited()


class DuplicateStageTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status='UC_DUP'):
        client = BitrixClient(webhook_url='https://example.com/rest/1/test/')
        client._call = AsyncMock(return_value=[
            {'STATUS_ID': 'UC_NLD47X', 'NAME': 'Мусор'},
            {'STATUS_ID': 'UC_DUP', 'NAME': 'Дубль'},
            {'STATUS_ID': 'UC_DUP_FINAL', 'NAME': 'Дубль', 'SEMANTICS': 'F'},
        ])
        client.update_lead = AsyncMock()
        client.get_lead = AsyncMock(return_value={'ID': '42', 'STATUS_ID': status})
        return client

    async def test_moves_lead_to_dubl_stage_not_junk(self):
        client = self.client()
        lead = await client.move_to_duplicate_stage('42')
        client.update_lead.assert_awaited_once_with('42', {'STATUS_ID': 'UC_DUP'})
        self.assertEqual(lead['STATUS_ID'], 'UC_DUP')

    async def test_rejects_silent_failure_to_change_stage(self):
        client = self.client(status='NEW')
        with self.assertRaisesRegex(BitrixApiError, 'не подтвердил'):
            await client.move_to_duplicate_stage('42')

    async def test_missing_stage_does_not_modify_lead(self):
        client = self.client()
        client._call.return_value = []
        with self.assertRaises(BitrixApiError):
            await client.move_to_duplicate_stage('42')
        client.update_lead.assert_not_awaited()
