import unittest
from unittest.mock import AsyncMock

from app.bitrix_client import BitrixClient


class DuplicateLeadTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return BitrixClient(webhook_url='https://example.com/rest/1/test/')

    async def test_find_duplicate_lead_ids_skips_call_without_phones(self):
        client = self.client()
        client._call = AsyncMock()
        result = await client.find_duplicate_lead_ids([])
        self.assertEqual(result, [])
        client._call.assert_not_awaited()

    async def test_find_duplicate_lead_ids_uses_native_bitrix_method(self):
        client = self.client()
        client._call = AsyncMock(return_value={'LEAD': ['10', '20']})
        result = await client.find_duplicate_lead_ids(['+79991234567'])
        client._call.assert_awaited_once_with(
            'crm.duplicate.findbycomm',
            {'entity_type': 'LEAD', 'type': 'PHONE', 'values': ['+79991234567']},
        )
        self.assertEqual(result, ['10', '20'])

    async def test_get_active_lead_ids_filters_by_semantic_status(self):
        client = self.client()
        client._call = AsyncMock(return_value=[
            {'ID': '10', 'STATUS_SEMANTIC_ID': 'P'},
            {'ID': '20', 'STATUS_SEMANTIC_ID': 'F'},
            {'ID': '30', 'STATUS_SEMANTIC_ID': 'S'},
        ])
        result = await client.get_active_lead_ids(['10', '20', '30'])
        self.assertEqual(result, ['10'])

    async def test_find_active_duplicate_lead_excludes_self_and_picks_latest(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['5', '9', '7'])
        client.get_active_lead_ids = AsyncMock(return_value=['5', '9'])
        result = await client.find_active_duplicate_lead(['+79991234567'], exclude_lead_id='7')
        client.get_active_lead_ids.assert_awaited_once_with(['5', '9'])
        self.assertEqual(result, '9')

    async def test_find_active_duplicate_lead_returns_none_without_active_matches(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['5'])
        client.get_active_lead_ids = AsyncMock(return_value=[])
        result = await client.find_active_duplicate_lead(['+79991234567'])
        self.assertIsNone(result)

    async def test_find_active_duplicate_lead_excludes_only_the_new_lead_itself(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['7'])
        client.get_active_lead_ids = AsyncMock(return_value=[])
        result = await client.find_active_duplicate_lead(['+79991234567'], exclude_lead_id='7')
        client.get_active_lead_ids.assert_awaited_once_with([])
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
