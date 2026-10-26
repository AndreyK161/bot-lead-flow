import unittest
from datetime import date
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.bitrix_client import BitrixClient


class DistributionBitrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_leads_by_moscow_calendar_day_and_stage(self):
        client = BitrixClient('https://example.test/rest/1/token/')
        client._call_payload = AsyncMock(return_value={
            'result': {'items': [
                {'id': 10, 'createdTime': '2026-10-19T23:30:00+03:00', 'stageId': 'IN_PROCESS'},
                {'id': 11, 'createdTime': '2026-10-20T00:30:00+03:00', 'stageId': 'PROCESSED'},
            ]},
        })
        days = [date(2026, 10, 19), date(2026, 10, 20)]
        result = await client.distribution_counts_by_stage(
            days, ['IN_PROCESS', 'PROCESSED'], timezone=ZoneInfo('Europe/Moscow'),
        )
        self.assertEqual(result[days[0]], {'IN_PROCESS': 1, 'PROCESSED': 0})
        self.assertEqual(result[days[1]], {'IN_PROCESS': 0, 'PROCESSED': 1})
        sent = client._call_payload.await_args.args[1]
        self.assertEqual(sent['filter']['@stageId'], ['IN_PROCESS', 'PROCESSED'])
        self.assertEqual(sent['filter']['>=createdTime'], '2026-10-19T00:00:00+03:00')
        self.assertEqual(sent['filter']['<createdTime'], '2026-10-21T00:00:00+03:00')

    async def test_empty_date_list_does_not_call_bitrix(self):
        client = BitrixClient('https://example.test/rest/1/token/')
        client._call_payload = AsyncMock()
        self.assertEqual(
            await client.distribution_counts([], ['IN_PROCESS'], timezone=ZoneInfo('Europe/Moscow')),
            {},
        )
        client._call_payload.assert_not_awaited()

    async def test_user_deals_are_read_only_active_and_scoped(self):
        client = BitrixClient('https://example.test/rest/1/token/')
        client._call_payload = AsyncMock(return_value={'result': []})
        await client.get_user_deals(110, 2)
        params = client._call_payload.await_args.args[1]
        self.assertEqual(params['filter'], {
            'ASSIGNED_BY_ID': '110', 'CATEGORY_ID': '2', 'CLOSED': 'N',
        })


if __name__ == '__main__':
    unittest.main()
