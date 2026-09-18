import unittest
from unittest.mock import AsyncMock

from app.bitrix_client import BitrixClient, BitrixApiError


class JunkStageTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status='UC_NLD47X'):
        client = BitrixClient(webhook_url='https://example.com/rest/1/test/')
        client._call = AsyncMock(return_value=[
            {'STATUS_ID': 'JUNK', 'NAME': 'Некачественный лид'},
            {'STATUS_ID': 'UC_NLD47X', 'NAME': 'Мусор'},
        ])
        client.update_lead = AsyncMock()
        client.get_lead = AsyncMock(return_value={'ID': '42', 'STATUS_ID': status})
        return client

    async def test_uses_real_stage_instead_of_unchecked_configuration(self):
        client = self.client()
        lead = await client.move_to_junk('42')
        client.update_lead.assert_awaited_once_with('42', {'STATUS_ID': 'UC_NLD47X'})
        self.assertEqual(lead['STATUS_ID'], 'UC_NLD47X')

    async def test_rejects_silent_failure_to_change_stage(self):
        client = self.client(status='NEW')
        with self.assertRaisesRegex(BitrixApiError, 'не подтвердил'):
            await client.move_to_junk('42')

    async def test_missing_stage_does_not_modify_lead(self):
        client = self.client()
        client._call.return_value = []
        with self.assertRaises(BitrixApiError):
            await client.move_to_junk('42')
        client.update_lead.assert_not_awaited()


class DuplicateStageTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status='UC_DUP'):
        client = BitrixClient(webhook_url='https://example.com/rest/1/test/')
        client._call = AsyncMock(return_value=[
            {'STATUS_ID': 'UC_NLD47X', 'NAME': 'Мусор'},
            {'STATUS_ID': 'UC_DUP', 'NAME': 'Дубль'},
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
