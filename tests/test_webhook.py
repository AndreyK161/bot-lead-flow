import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main, manual, store
from app.bitrix_client import BitrixApiError


def form_request(headers=None, **fields):
    return SimpleNamespace(headers=headers or {}, form=AsyncMock(return_value=fields))


def json_request(headers=None, **body):
    return SimpleNamespace(headers=headers or {}, json=AsyncMock(return_value=body))


class WebhookTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            database_path=self.tmp.name + '/db.sqlite',
            bitrix_webhook_url='https://example.com/rest/1/test/',
            bitrix_application_token='apptoken',
            telegram_chat_id='-100',
            telegram_webhook_secret='secret',
            director_user_id_set={100},
            admin_user_id_set={200},
            sales_department_id='5',
            manual_bot_token='',
        )
        self.patches = [
            patch('app.main.get_settings', return_value=self.settings),
            patch('app.store.get_settings', return_value=self.settings),
            patch('app.manual.get_settings', return_value=self.settings),
        ]
        for p in self.patches:
            p.start()
        manual.lock = manual.lock.__class__()

        self.client = AsyncMock()
        self.client.get_lead.return_value = {'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': ''}
        self.client.get_source_name.return_value = 'Сайт'
        self.client.get_user_name.return_value = 'Никита Продажников'
        self.client.get_department_users.return_value = [{'ID': '460', 'NAME': 'Никита', 'LAST_NAME': 'Продажников'}]
        self.bitrix_patch = patch('app.main.BitrixClient', return_value=self.client)
        self.bitrix_patch.start()

        self.send_message = AsyncMock(return_value={'message_id': 42, 'chat': {'id': -100}})
        self.edit_markup = AsyncMock()
        self.edit_text = AsyncMock()
        self.answer_cb = AsyncMock()
        self.tg_patches = [
            patch('app.main.send_telegram_message', new=self.send_message),
            patch('app.main.edit_message_reply_markup', new=self.edit_markup),
            patch('app.main.edit_message_text', new=self.edit_text),
            patch('app.main.answer_callback_query', new=self.answer_cb),
        ]
        for p in self.tg_patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.tg_patches:
            p.stop()
        self.bitrix_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()


class BitrixWebhookTests(WebhookTestCase):
    async def test_rejects_wrong_application_token(self):
        request = form_request(**{'auth[application_token]': 'wrong', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '1'})
        with self.assertRaises(Exception) as ctx:
            await main.bitrix_webhook(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_ignores_other_events(self):
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMCONTACTADD'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ignored'})
        self.client.get_lead.assert_not_awaited()

    async def test_routes_new_deal_event_to_deal_tracker(self):
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMDEALADD', 'data[FIELDS][ID]': '19406'})
        with patch('app.main.deals.track', new=AsyncMock(return_value=True)) as track:
            result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        track.assert_awaited_once_with('19406')

    async def test_sends_dm_to_every_director(self):
        self.settings.director_user_id_set = {100, 101}
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.assertEqual(self.send_message.await_count, 2)
        chat_ids = {call.kwargs['chat_id'] for call in self.send_message.call_args_list}
        self.assertEqual(chat_ids, {100, 101})

    async def test_one_director_delivery_failure_does_not_block_others(self):
        self.settings.director_user_id_set = {100, 101}
        self.send_message.side_effect = [RuntimeError('bot cannot initiate chat'), {'message_id': 1}]
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.assertEqual(self.send_message.await_count, 2)

    async def test_skips_leads_created_by_manual_bot(self):
        self.client.get_lead.return_value = {'ID': '1', 'SOURCE_DESCRIPTION': manual.MARKER + 'abc'}
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '1'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'manual'})
        self.send_message.assert_not_awaited()

    async def test_bitrix_error_returns_502(self):
        self.client.get_lead.side_effect = BitrixApiError('boom')
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '1'})
        with self.assertRaises(Exception) as ctx:
            await main.bitrix_webhook(request)
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_duplicate_lead_is_auto_junked_and_flagged(self):
        self.client.get_lead.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': '',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        self.client.find_active_duplicate_lead.return_value = '999'
        self.client.move_to_junk.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'STATUS_ID': 'JUNK',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.client.find_active_duplicate_lead.assert_awaited_once_with(['+79991234567'], exclude_lead_id='20312')
        self.client.move_to_junk.assert_awaited_once_with('20312')
        text = self.send_message.call_args.args[0]
        self.assertIn('Дубликат', text)
        self.assertIn('№999', text)

    async def test_non_duplicate_lead_is_not_junked(self):
        self.client.get_lead.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': '',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        self.client.find_active_duplicate_lead.return_value = None
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.client.move_to_junk.assert_not_awaited()
        text = self.send_message.call_args.args[0]
        self.assertNotIn('Дубликат', text)

    async def test_no_phone_skips_duplicate_check(self):
        self.client.get_lead.return_value = {'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': ''}
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        await main.bitrix_webhook(request)
        self.client.find_active_duplicate_lead.assert_not_awaited()


class TelegramWebhookAuthTests(WebhookTestCase):
    async def test_rejects_wrong_secret(self):
        request = json_request(headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'})
        with self.assertRaises(Exception) as ctx:
            await main.telegram_webhook(request)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_start_records_and_confirms(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={'chat': {'id': 555, 'type': 'private'}, 'from': {'id': 555, 'username': 'ivan', 'first_name': 'Иван'}, 'text': '/start'},
        )
        await main.telegram_webhook(request)
        self.assertEqual(store.recent_starts()[0]['telegram_id'], 555)
        self.send_message.assert_awaited_once()

    async def test_group_messages_are_ignored(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={'chat': {'id': -100, 'type': 'group'}, 'from': {'id': 555}, 'text': '/start'},
        )
        await main.telegram_webhook(request)
        self.assertEqual(store.recent_starts(), [])

    async def test_phone_message_is_routed_to_manual_flow(self):
        message={'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}, 'text': '+79990000000'}
        request=json_request(headers={'X-Telegram-Bot-Api-Secret-Token':'secret'},update_id=77,message=message)
        with patch('app.main.manual.handle_main_message',new=AsyncMock(return_value=True)) as handle:
            result=await main.telegram_webhook(request)
        self.assertEqual(result,{'status':'ok'})
        handle.assert_awaited_once_with(message,77)

    async def test_manual_callback_is_acknowledged_before_processing(self):
        callback={'id':'cb-manual','from':{'id':100},'data':'ms:abc:tg','message':{'chat':{'id':100},'message_id':1}}
        request=json_request(headers={'X-Telegram-Bot-Api-Secret-Token':'secret'},update_id=78,callback_query=callback)
        with patch('app.main.manual.handle_main_callback',new=AsyncMock(return_value=True)) as handle:
            await main.telegram_webhook(request)
        self.answer_cb.assert_awaited_once_with('cb-manual')
        handle.assert_awaited_once_with(callback,78)


class RoleSeparationTests(WebhookTestCase):
    async def test_director_cannot_use_link_command(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={'chat': {'id': 100, 'type': 'private'}, 'from': {'id': 100}, 'text': '/link'},
        )
        await main.telegram_webhook(request)
        self.send_message.assert_not_awaited()
        self.client.get_department_users.assert_not_awaited()

    async def test_admin_can_use_link_command(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={'chat': {'id': 200, 'type': 'private'}, 'from': {'id': 200}, 'text': '/link'},
        )
        await main.telegram_webhook(request)
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], 200)

    async def test_admin_cannot_use_lead_management_callback(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 200}, 'data': 'm:42',
                            'message': {'chat': {'id': 200}, 'message_id': 1, 'text': 'x'}},
        )
        await main.telegram_webhook(request)
        self.answer_cb.assert_awaited_once_with('cb1', text='Нет доступа', show_alert=True)
        self.edit_markup.assert_not_awaited()

    async def test_director_cannot_use_link_callback(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 100}, 'data': 'link_pick:460',
                            'message': {'chat': {'id': 100}, 'message_id': 1, 'text': 'x'}},
        )
        await main.telegram_webhook(request)
        self.answer_cb.assert_awaited_once_with('cb1', text='Нет доступа', show_alert=True)
        self.client.get_user_name.assert_not_awaited()

    async def test_unknown_user_gets_no_access_on_callback(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 999}, 'data': 'm:42',
                            'message': {'chat': {'id': 999}, 'message_id': 1, 'text': 'x'}},
        )
        await main.telegram_webhook(request)
        self.answer_cb.assert_awaited_once_with('cb1', text='Нет доступа', show_alert=True)


class LinkFlowTests(WebhookTestCase):
    async def test_full_link_cycle_stores_mapping(self):
        store.record_start(555, 'ivan', 'Иван')

        pick_request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 200}, 'data': 'link_pick:460',
                            'message': {'chat': {'id': 200}, 'message_id': 10, 'text': 'x'}},
        )
        await main.telegram_webhook(pick_request)
        self.edit_text.assert_awaited_once()
        keyboard = self.edit_text.call_args.kwargs['reply_markup']
        self.assertIn('link_to:460:555', keyboard['inline_keyboard'][0][0]['callback_data'])

        to_request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb2', 'from': {'id': 200}, 'data': 'link_to:460:555',
                            'message': {'chat': {'id': 200}, 'message_id': 10, 'text': 'x'}},
        )
        await main.telegram_webhook(to_request)
        self.assertEqual(store.manager_telegram_id('460'), 555)

    async def test_link_pick_with_no_starts_shows_empty_state(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 200}, 'data': 'link_pick:460',
                            'message': {'chat': {'id': 200}, 'message_id': 10, 'text': 'x'}},
        )
        await main.telegram_webhook(request)
        self.edit_text.assert_awaited_once()
        self.assertIn('никто не писал', self.edit_text.call_args.args[2])


class AssignAndJunkTests(WebhookTestCase):
    async def test_assign_notifies_linked_manager(self):
        store.link_manager('460', 'Никита Продажников', 555)
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 100}, 'data': 'au:20312:460',
                            'message': {'chat': {'id': 100}, 'message_id': 10, 'text': 'card text'}},
        )
        await main.telegram_webhook(request)
        self.client.update_lead.assert_awaited_once_with('20312', {'ASSIGNED_BY_ID': '460'})
        chat_ids = [call.kwargs.get('chat_id') for call in self.send_message.call_args_list]
        self.assertIn(555, chat_ids)

    async def test_assign_to_user_outside_department_is_rejected(self):
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 100}, 'data': 'au:20312:999',
                            'message': {'chat': {'id': 100}, 'message_id': 10, 'text': 'card text'}},
        )
        await main.telegram_webhook(request)
        self.client.update_lead.assert_not_awaited()
        self.answer_cb.assert_awaited_once_with('cb1', text='Сотрудник больше не входит в отдел', show_alert=True)

    async def test_junk_clears_keyboard(self):
        self.client.move_to_junk.return_value = {'ID': '20312', 'STATUS_ID': 'UC_NLD47X'}
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 100}, 'data': 'j:20312',
                            'message': {'chat': {'id': 100}, 'message_id': 10, 'text': 'card text'}},
        )
        await main.telegram_webhook(request)
        self.client.move_to_junk.assert_awaited_once_with('20312')
        self.assertEqual(self.edit_text.call_args.kwargs['reply_markup'], {'inline_keyboard': []})


if __name__ == '__main__':
    unittest.main()
