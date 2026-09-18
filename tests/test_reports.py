import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import reports, store
from app.formatter import build_daily_report_body


class BuildDailyReportBodyTests(unittest.TestCase):
    def test_groups_leads_with_same_source_and_stage_into_one_clause(self):
        leads = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
            {'ID': '2', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
        ]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'TG': 'Телеграм'}, status_map={'JUNK': 'Мусор'},
        )
        self.assertEqual(text, 'Телеграм — <a href="https://example.bitrix24.ru/crm/lead/details/1/">Лид №1</a>, '
                                '<a href="https://example.bitrix24.ru/crm/lead/details/2/">Лид №2</a> : «Мусор»\n\nВсего: 2')

    def test_no_per_group_total_suffix_anymore(self):
        leads = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
            {'ID': '2', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
        ]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'TG': 'Телеграм'}, status_map={'JUNK': 'Мусор'},
        )
        self.assertNotIn('(всего:', text)

    def test_bottom_total_line_counts_all_leads_in_report(self):
        leads = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
            {'ID': '2', 'SOURCE_ID': 'TG', 'STATUS_ID': 'NEW'},
            {'ID': '3', 'SOURCE_ID': 'YANDEX', 'STATUS_ID': 'JUNK'},
        ]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'TG': 'Телеграм', 'YANDEX': 'Яндекс'}, status_map={'JUNK': 'Мусор', 'NEW': 'Не обработан'},
        )
        self.assertTrue(text.endswith('Всего: 3'))

    def test_different_stages_within_one_source_are_grouped_separately(self):
        leads = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
            {'ID': '2', 'SOURCE_ID': 'TG', 'STATUS_ID': 'NEW'},
            {'ID': '3', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
        ]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'TG': 'Телеграм'}, status_map={'JUNK': 'Мусор', 'NEW': 'Не обработан'},
        )
        # non-adjacent leads with the same stage (1 and 3) still end up in one clause
        self.assertEqual(text.count('Лид №1</a>, <a href="https://example.bitrix24.ru/crm/lead/details/3/">Лид №3</a> : «Мусор»'), 1)
        self.assertIn('«Не обработан»', text)
        self.assertEqual(text.count('Телеграм —'), 1)

    def test_multiple_sources_produce_separate_lines(self):
        leads = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK'},
            {'ID': '2', 'SOURCE_ID': 'YANDEX', 'STATUS_ID': 'JUNK'},
        ]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'TG': 'Телеграм', 'YANDEX': 'Яндекс'}, status_map={'JUNK': 'Мусор'},
        )
        source_lines = [line for line in text.split('\n') if line and not line.startswith('Всего')]
        self.assertEqual(len(source_lines), 2)

    def test_unknown_source_and_status_fall_back_to_raw_id(self):
        leads = [{'ID': '1', 'SOURCE_ID': 'WEIRD', 'STATUS_ID': 'CUSTOM'}]
        text = build_daily_report_body(leads, portal_domain='example.bitrix24.ru', source_map={}, status_map={})
        self.assertIn('WEIRD', text)
        self.assertIn('CUSTOM', text)

    def test_html_in_names_is_escaped(self):
        leads = [{'ID': '1', 'SOURCE_ID': 'X', 'STATUS_ID': 'Y'}]
        text = build_daily_report_body(
            leads, portal_domain='example.bitrix24.ru',
            source_map={'X': '<script>'}, status_map={'Y': 'a & b'},
        )
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('a &amp; b', text)


class SendDailyReportsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + '/db.sqlite',
            bitrix_webhook_url='https://example.bitrix24.ru/rest/1/test/',
            director_user_id_set={100, 101},
        )
        self.patches = [
            patch('app.store.get_settings', return_value=self.settings),
            patch('app.reports.get_settings', return_value=self.settings),
        ]
        for p in self.patches:
            p.start()

        self.client = AsyncMock()
        self.client.get_sources.return_value = [{'STATUS_ID': 'TG', 'NAME': 'Телеграм'}]
        self.client.get_lead_statuses.return_value = [{'STATUS_ID': 'JUNK', 'NAME': 'Мусор'}]
        self.client.get_user_name.return_value = 'Никита Продажников'
        self.bitrix_patch = patch('app.reports.BitrixClient', return_value=self.client)
        self.bitrix_patch.start()

        self.send_message = AsyncMock(return_value={'message_id': 1})
        self.send_patch = patch('app.reports.send_telegram_message', new=self.send_message)
        self.send_patch.start()

    async def asyncTearDown(self):
        self.send_patch.stop()
        self.bitrix_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def test_no_leads_today_sends_nothing(self):
        self.client.get_leads_created_between.return_value = []
        await reports.send_daily_reports()
        self.send_message.assert_not_awaited()

    async def test_linked_manager_gets_personal_report(self):
        store.link_manager('460', 'Никита Продажников', 555)
        self.client.get_leads_created_between.return_value = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK', 'ASSIGNED_BY_ID': '460'},
        ]
        await reports.send_daily_reports()
        chat_ids = [call.kwargs['chat_id'] for call in self.send_message.call_args_list]
        self.assertIn(555, chat_ids)
        personal = next(call for call in self.send_message.call_args_list if call.kwargs['chat_id'] == 555)
        self.assertIn('Отчёт за', personal.args[0])
        self.assertIn('Лид №1', personal.args[0])

    async def test_unlinked_manager_gets_no_personal_dm_but_appears_in_digest(self):
        self.client.get_leads_created_between.return_value = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK', 'ASSIGNED_BY_ID': '999'},
        ]
        await reports.send_daily_reports()
        chat_ids = {call.kwargs['chat_id'] for call in self.send_message.call_args_list}
        self.assertEqual(chat_ids, {100, 101})
        digest = next(call for call in self.send_message.call_args_list if call.kwargs['chat_id'] == 100)
        self.assertIn('Никита Продажников', digest.args[0])
        self.assertIn('Сводный отчёт', digest.args[0])

    async def test_leads_without_assignee_are_skipped(self):
        self.client.get_leads_created_between.return_value = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK', 'ASSIGNED_BY_ID': ''},
        ]
        await reports.send_daily_reports()
        self.send_message.assert_not_awaited()

    async def test_every_director_receives_the_digest(self):
        self.client.get_leads_created_between.return_value = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK', 'ASSIGNED_BY_ID': '460'},
        ]
        await reports.send_daily_reports()
        chat_ids = {call.kwargs['chat_id'] for call in self.send_message.call_args_list}
        self.assertEqual(chat_ids, {100, 101})

    async def test_one_recipient_failure_does_not_block_others(self):
        store.link_manager('460', 'Никита Продажников', 555)
        self.client.get_leads_created_between.return_value = [
            {'ID': '1', 'SOURCE_ID': 'TG', 'STATUS_ID': 'JUNK', 'ASSIGNED_BY_ID': '460'},
        ]
        self.send_message.side_effect = [RuntimeError('boom'), {'message_id': 1}, {'message_id': 2}]
        await reports.send_daily_reports()
        self.assertEqual(self.send_message.await_count, 3)


class TodayBoundsTests(unittest.TestCase):
    def test_bounds_span_exactly_one_moscow_day(self):
        now = datetime(2026, 9, 18, 21, 30, tzinfo=reports.MOSCOW_TZ)
        start, end = reports._today_bounds_moscow(now)
        self.assertTrue(start.startswith('2026-09-18T00:00:00'))
        self.assertTrue(end.startswith('2026-09-19T00:00:00'))


if __name__ == '__main__':
    unittest.main()
