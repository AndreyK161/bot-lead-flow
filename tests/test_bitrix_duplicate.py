import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.bot.backend import store
from apps.bot.backend.bitrix_client import BitrixApiError, BitrixClient


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

    async def test_phone_duplicate_context_includes_contact_and_lead_deals(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['10'])
        client.find_duplicate_contact_ids = AsyncMock(return_value=['30'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '10', 'CONTACT_ID': '20'},
        ])
        client.get_deals_by_lead_ids = AsyncMock(return_value=[
            {'ID': '100', 'LEAD_ID': '10'},
        ])
        client.get_deals_by_contact_ids = AsyncMock(return_value=[
            {'ID': '100', 'CONTACT_ID': '20'},
            {'ID': '101', 'CONTACT_ID': '30'},
        ])

        context = await client.find_phone_duplicate_context(['+79991234567'])

        self.assertEqual([item['ID'] for item in context['leads']], ['10'])
        self.assertEqual(
            {item['ID'] for item in context['deals']}, {'100', '101'},
        )
        contact_ids = set(client.get_deals_by_contact_ids.call_args.args[0])
        self.assertEqual(contact_ids, {'20', '30'})

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
        (queried,), _ = client.get_active_lead_ids.call_args
        self.assertEqual(sorted(queried), ['5', '9'])
        self.assertEqual(result, '9')

    async def test_origin_prefers_earliest_active_and_excludes_legacy_duplicates(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['9', '5', '7'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '9', 'STATUS_ID': 'IN_PROCESS', 'STATUS_SEMANTIC_ID': 'P'},
            {'ID': '5', 'STATUS_ID': 'IN_PROCESS', 'STATUS_SEMANTIC_ID': 'P'},
            {'ID': '7', 'STATUS_ID': 'UC_DUP', 'STATUS_SEMANTIC_ID': 'P'},
        ])
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'IN_PROCESS', 'NAME': 'В работе'},
            {'STATUS_ID': 'UC_DUP', 'NAME': 'Дубль'},
        ])

        result = await client.find_duplicate_origin_lead(
            ['+79991234567'], exclude_lead_id='10',
        )

        self.assertEqual(result, '5')

    async def test_converted_origin_is_kept_for_related_deal_notification(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['5'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '5', 'STATUS_ID': 'CONVERTED', 'STATUS_SEMANTIC_ID': 'S'},
        ])
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'CONVERTED', 'NAME': 'Качественный лид'},
        ])

        result = await client.find_duplicate_origin_lead(['+79991234567'])

        self.assertEqual(result, '5')

    async def test_origin_is_earliest_even_when_it_is_converted(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['9', '5'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '9', 'STATUS_ID': 'IN_PROCESS', 'STATUS_SEMANTIC_ID': 'P'},
            {'ID': '5', 'STATUS_ID': 'CONVERTED', 'STATUS_SEMANTIC_ID': 'S'},
        ])
        client.get_lead_statuses = AsyncMock(return_value=[])

        result = await client.find_duplicate_origin_lead(['+79991234567'])

        self.assertEqual(result, '5')

    async def test_failed_lead_still_makes_new_request_a_duplicate(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['5', '7'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '5', 'STATUS_ID': 'JUNK', 'STATUS_SEMANTIC_ID': 'F'},
            {'ID': '7', 'STATUS_ID': 'UC_DUP', 'STATUS_SEMANTIC_ID': 'P'},
        ])
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'JUNK', 'NAME': 'Некачественный лид'},
            {'STATUS_ID': 'UC_DUP', 'NAME': 'Дубль'},
        ])

        result = await client.find_duplicate_origin_lead(['+79991234567'])

        self.assertEqual(result, '5')

    async def test_legacy_duplicate_history_is_used_when_no_other_origin_exists(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['9', '7'])
        client.get_leads_by_ids = AsyncMock(return_value=[
            {'ID': '9', 'STATUS_ID': 'UC_DUP', 'STATUS_SEMANTIC_ID': 'P'},
            {'ID': '7', 'STATUS_ID': 'UC_DUP', 'STATUS_SEMANTIC_ID': 'P'},
        ])
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'UC_DUP', 'NAME': 'Дубль'},
        ])

        result = await client.find_duplicate_origin_lead(['+79991234567'])

        self.assertEqual(result, '7')

    async def test_duplicate_is_failed_with_reason_from_bitrix_enumeration(self):
        client = self.client()
        settings = SimpleNamespace(
            lead_failed_status_id='JUNK',
            lead_failure_reason_field='UF_REASON',
            lead_duplicate_reason_name='Дубль',
        )
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'NEW', 'NAME': 'Не обработан'},
            {'STATUS_ID': 'JUNK', 'NAME': 'Некачественный лид', 'SEMANTICS': 'F'},
        ])
        client._call_all = AsyncMock(return_value=[{
            'FIELD_NAME': 'UF_REASON',
            'LIST': [
                {'ID': '1', 'VALUE': 'Мусор'},
                {'ID': '2118', 'VALUE': 'Дубль'},
            ],
        }])
        client.update_lead = AsyncMock()
        client.get_lead = AsyncMock(return_value={
            'ID': '42', 'STATUS_ID': 'JUNK', 'UF_REASON': '2118',
        })

        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=settings):
            lead = await client.fail_duplicate_lead('42')

        client.update_lead.assert_awaited_once_with('42', {
            'STATUS_ID': 'JUNK', 'UF_REASON': '2118',
        })
        self.assertEqual(lead['UF_REASON'], '2118')

    async def test_duplicate_failure_requires_configured_reason(self):
        client = self.client()
        settings = SimpleNamespace(
            lead_failed_status_id='JUNK',
            lead_failure_reason_field='UF_REASON',
            lead_duplicate_reason_name='Дубль',
        )
        client.get_lead_statuses = AsyncMock(return_value=[
            {'STATUS_ID': 'JUNK', 'SEMANTICS': 'F'},
        ])
        client._call_all = AsyncMock(return_value=[{
            'FIELD_NAME': 'UF_REASON', 'LIST': [{'ID': '1', 'VALUE': 'Мусор'}],
        }])
        client.update_lead = AsyncMock()

        with patch('apps.bot.backend.bitrix_client.get_settings', return_value=settings):
            with self.assertRaisesRegex(BitrixApiError, 'Дубль'):
                await client.fail_duplicate_lead('42')
        client.update_lead.assert_not_awaited()

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

    async def test_extra_candidate_ids_from_local_cache_are_included(self):
        """Локальный кэш (нормализация +7/8) дополняет поиск Bitrix, который может пропустить такие варианты."""
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=[])
        client.get_active_lead_ids = AsyncMock(return_value=['555'])
        result = await client.find_active_duplicate_lead(['+79991234567'], extra_candidate_ids=['555'])
        (queried,), _ = client.get_active_lead_ids.call_args
        self.assertEqual(queried, ['555'])
        self.assertEqual(result, '555')

    async def test_extra_candidate_ids_deduplicated_with_bitrix_results(self):
        client = self.client()
        client.find_duplicate_lead_ids = AsyncMock(return_value=['555'])
        client.get_active_lead_ids = AsyncMock(return_value=['555'])
        await client.find_active_duplicate_lead(['+79991234567'], extra_candidate_ids=['555'])
        (queried,), _ = client.get_active_lead_ids.call_args
        self.assertEqual(queried, ['555'])


class PlusSevenVsEightIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Воспроизводит реальную ситуацию: +79241643860 vs +89241643860 — Bitrix их дублями не считает,
    но локальный кэш (нормализация по последним 10 цифрам) — считает."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(database_path=self.tmp.name + '/db.sqlite')
        self.patcher = patch('apps.bot.backend.store.get_settings', return_value=self.settings)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_bitrix_missing_the_match_is_caught_by_local_cache(self):
        store.record_seen_lead({'ID': '111', 'PHONE': [{'VALUE': '+79241643860'}]})
        client = self.client()
        # Bitrix's own findbycomm did not recognize +8... as the same number as +7...
        client._call = AsyncMock(return_value={'LEAD': []})
        client.get_active_lead_ids = AsyncMock(return_value=['111'])
        result = await client.find_active_duplicate_lead(
            ['+89241643860'],
            exclude_lead_id='222',
            extra_candidate_ids=store.find_all_seen_lead_ids_by_phones(['+89241643860']),
        )
        self.assertEqual(result, '111')

    def client(self):
        return BitrixClient(webhook_url='https://example.com/rest/1/test/')


if __name__ == '__main__':
    unittest.main()
