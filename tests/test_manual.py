import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app import manual, store, main
from app.bitrix_client import BitrixApiError


class ManualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(database_path=self.tmp.name+'/db.sqlite', director_user_id_set={123}, admin_user_id_set=set(),
            manual_bot_token='test', manual_webhook_secret='secret', telegram_chat_id='-100',
            notification_chat_ids=['-100'], track_deal_category_id='0',
            bitrix_webhook_url='https://example.com/rest/1/test/', sales_department_id='5')
        self.patches = [patch('app.store.get_settings', return_value=self.settings),
                        patch('app.manual.get_settings', return_value=self.settings),
                        patch('app.main.get_settings', return_value=self.settings),
                        patch('app.deals.get_settings', return_value=self.settings)]
        for p in self.patches: p.start()
        self.client = AsyncMock()
        self.client.get_sources.return_value = [{'STATUS_ID': 'vk', 'NAME': 'Вконтакте'}, {'STATUS_ID': 'max', 'NAME': 'Max Станислава'}, {'STATUS_ID': 'yandex', 'NAME': 'Яндекс'}]
        self.client.add_lead.return_value = '42'
        self.client.get_lead.return_value = {'ID': '42', 'SOURCE_ID': 'vk', 'SOURCE_DESCRIPTION': manual.MARKER+'test'}
        self.client.move_to_junk.return_value = self.client.get_lead.return_value
        self.client.get_department_users.return_value = [{'ID': '7', 'NAME': 'Иван'}]
        self.client.get_user_name.return_value = 'Иван'
        self.client.get_source_name.return_value = 'Вконтакте'
        self.client.find_active_duplicate_lead.return_value = None
        self.patches += [patch('app.manual.BitrixClient', return_value=self.client), patch('app.main.BitrixClient', return_value=self.client),
                         patch('app.manual.call', new=AsyncMock(return_value={'result': {'message_id': 10}})),
                         patch('app.manual.send_telegram_message', new=AsyncMock(return_value={'message_id': 99})),
                         patch('app.manual.edit_message_text', new=AsyncMock()),
                         patch('app.main.edit_message_text', new=AsyncMock()), patch('app.main.answer_callback_query', new=AsyncMock())]
        self.patches += [patch('app.deals.BitrixClient', return_value=self.client), patch('app.deals.edit_message_text', new=AsyncMock())]
        for p in self.patches[4:]: p.start()
        manual.lock = asyncio.Lock()
        from app import deals
        deals.lock = asyncio.Lock()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def message(self, uid=1, text='@client', user=123):
        return {'update_id': uid, 'message': {'from': {'id': user}, 'chat': {'id':123, 'type':'private'}, 'message_id':1, 'text':text}}

    def callback(self, action, row, uid=2):
        return {'update_id':uid, 'callback_query': {'id':str(uid), 'from':{'id':123}, 'data':f'{action}:{row["id"]}', 'message': {'chat':{'id':123,'type':'private'}, 'message_id':10}}}

    async def draft(self):
        await manual.handle(self.message())
        return store.rows('SELECT * FROM submissions')[0]

    async def submitted(self):
        row = await self.draft()
        await manual.handle(self.callback('select', row) | {'callback_query':self.callback('select', row)['callback_query'] | {'data':f'select:{row["id"]}:vk'}})
        return store.submission(row['id'])

    async def test_submission_retry_does_not_duplicate_crm(self):
        row = await self.submitted()
        await manual.handle(self.callback('select', row) | {'callback_query':self.callback('select', row)['callback_query'] | {'data':f'select:{row["id"]}:vk'}})
        self.client.add_lead.assert_awaited_once()
        self.assertEqual(row['main_message_id'],99)
        fields = self.client.add_lead.call_args.args[0]
        self.assertNotIn('PHONE', fields)
        self.assertTrue(fields['SOURCE_DESCRIPTION'].startswith(manual.MARKER))

    async def test_cancel_creates_no_lead(self):
        row = await self.draft()
        await manual.handle(self.callback('cancel',row))
        self.assertEqual(store.submission(row['id'])['state'],'cancelled')
        self.client.add_lead.assert_not_awaited()

    async def test_junk_requires_second_press_and_preserves_lead(self):
        row = await self.submitted()
        await manual.handle(self.callback('delete',row))
        self.client.delete_lead.assert_not_awaited()
        await manual.handle(self.callback('confirm',row,3))
        await manual.handle(self.callback('confirm',row,3))
        self.client.delete_lead.assert_not_awaited()
        self.client.move_to_junk.assert_awaited_once_with('42')
        self.assertEqual(store.submission(row['id'])['state'],'junk')
        manual.edit_message_text.assert_awaited_once()
        self.assertIn('<a href="https://example.com/crm/lead/details/42/">', manual.edit_message_text.call_args.args[2])

    async def test_main_junk_preserves_crm_link_and_updates_manual_card(self):
        row = await self.submitted()
        cb = self.callback('j', {'id':'42'})['callback_query']
        cb['message']['text'] = 'Новый лид\nОткрыть лид в CRM'
        self.settings.telegram_webhook_secret = 'main-secret'
        request = SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token':'main-secret'}, json=AsyncMock(return_value={'callback_query':cb}))
        await main.telegram_webhook(request)
        self.client.move_to_junk.assert_awaited_once_with('42')
        self.client.delete_lead.assert_not_awaited()
        text = main.edit_message_text.call_args.args[2]
        self.assertIn('<a href="https://example.com/crm/lead/details/42/">', text)
        self.assertIn('Отправлен на стадию «Мусор»',text)
        self.assertNotIn('Новый лид',text)
        self.assertEqual(store.submission(row['id'])['state'],'junk')

    async def test_main_deal_assignment_and_junk_actions(self):
        from app import deals
        store.execute('INSERT INTO deal_notifications VALUES (?,0)',('77',))
        store.execute('INSERT INTO deal_deliveries VALUES (?,?,?)',('77','123',55))
        deal={'ID':'77','TITLE':'Сделка','CATEGORY_ID':'0','STAGE_ID':'NEW','SOURCE_ID':'WEB','ASSIGNED_BY_ID':'7'}
        self.client.get_deal.return_value=deal
        self.client.move_deal_to_junk.return_value=deal | {'STAGE_ID':'UC_K0Z3P6'}
        self.client.get_department_users.return_value=[{'ID':'7','NAME':'Иван'}]
        self.client.get_user_name.return_value='Иван'
        self.client.get_contact.return_value={}
        self.settings.telegram_webhook_secret='main-secret'
        for data in ('dau:77:7','dj:77'):
            cb=self.callback(data.split(':')[0],{'id':':'.join(data.split(':')[1:])})['callback_query']
            cb['message']['text']='Сделка'
            request=SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token':'main-secret'},json=AsyncMock(return_value={'callback_query':cb}))
            await main.telegram_webhook(request)
        self.client.update_deal.assert_awaited_once_with('77',{'ASSIGNED_BY_ID':'7'})
        self.client.move_deal_to_junk.assert_awaited_once_with('77')
        texts=[call.args[2] for call in deals.edit_message_text.call_args_list]
        self.assertTrue(any('Сделка отправлена на стадию «Мусор»' in text for text in texts))

    async def test_sources_and_retry_toggle_persist(self):
        await manual.refresh_sources()
        self.assertEqual({s['id'] for s in store.rows('SELECT * FROM sources WHERE enabled=1')},{'vk','max'})
        update = self.callback('st',{'id':'vk:0'})
        await manual.handle(update)
        await manual.handle(update)
        self.assertEqual(store.rows('SELECT enabled FROM sources WHERE id=?',('vk',))[0]['enabled'],0)

    async def test_unauthorized_and_duplicate_message(self):
        await manual.handle(self.message(user=999))
        self.assertFalse(store.rows('SELECT * FROM submissions'))
        await self.draft()
        await manual.handle(self.message())
        self.assertEqual(len(store.rows('SELECT * FROM submissions')),1)

    async def test_assign_updates_manual_card(self):
        row = await self.submitted()
        cb = self.callback('au',{'id':'42:7'})['callback_query']
        cb['message']['text'] = 'Заявка'
        request = SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token':'main-secret'}, json=AsyncMock(return_value={'callback_query':cb}))
        self.settings.telegram_webhook_secret='main-secret'
        await main.telegram_webhook(request)
        self.assertEqual(store.submission(row['id'])['manager'],'Иван')
        self.assertIn('Иван', manual.card(store.submission(row['id']))[0])

    async def test_timeout_recovers_existing_lead_without_second_create(self):
        row = await self.draft()
        self.client.add_lead.side_effect = httpx.ReadTimeout('timeout')
        update=self.callback('select',row)
        update['callback_query']['data'] += ':vk'
        await manual.handle(update)
        self.assertEqual(store.submission(row['id'])['state'],'uncertain')
        self.client._call.return_value = [{'ID':'42'}]
        await manual.recover()
        self.assertEqual(store.submission(row['id'])['state'],'submitted')
        self.client.add_lead.assert_awaited_once()

    async def test_bitrix_rejection_can_be_cancelled(self):
        row = await self.draft()
        self.client.add_lead.side_effect=BitrixApiError('access denied')
        update=self.callback('select',row)
        update['callback_query']['data'] += ':vk'
        await manual.handle(update)
        self.assertEqual(store.submission(row['id'])['state'],'draft')
        await manual.handle(self.callback('cancel',row,3))
        self.assertEqual(store.submission(row['id'])['state'],'cancelled')

    async def test_new_recipient_gets_existing_lead_without_resending_primary(self):
        row = await self.submitted()
        manual.send_telegram_message.reset_mock()
        self.settings.notification_chat_ids = ['-100', '123']
        await manual.recover()
        manual.send_telegram_message.assert_awaited_once()
        self.assertEqual(manual.send_telegram_message.call_args.kwargs['chat_id'], '123')
        await manual.recover()
        manual.send_telegram_message.assert_awaited_once()
        self.client.add_lead.assert_awaited_once()

    async def test_partial_delivery_retries_only_missing_recipient(self):
        row = await self.submitted()
        self.settings.notification_chat_ids = ['-100', '123']
        manual.send_telegram_message.reset_mock()
        manual.send_telegram_message.side_effect = RuntimeError('temporary Telegram failure')
        await manual.recover()
        self.assertEqual(len(manual.deliveries(row)), 1)
        manual.send_telegram_message.side_effect = None
        await manual.recover()
        self.assertEqual(len(manual.deliveries(row)), 2)
        self.assertEqual([call.kwargs['chat_id'] for call in manual.send_telegram_message.call_args_list], ['123', '123'])

    async def test_unconfirmed_junk_does_not_mark_card_as_successful(self):
        row = await self.submitted()
        self.client.move_to_junk.side_effect = BitrixApiError('Битрикс не подтвердил перенос')
        await manual.handle(self.callback('confirm',row))
        self.assertEqual(store.submission(row['id'])['state'], 'submitted')
        self.assertNotIn('Отправлен на стадию',manual.card(store.submission(row['id']))[0])

    async def _draft_with_phone(self, phone='+79991234567'):
        await manual.handle(self.message(text=phone))
        return store.rows('SELECT * FROM submissions')[0]

    async def _select_source(self, row, uid=3):
        cb = self.callback('select', row, uid)
        cb['callback_query']['data'] += ':vk'
        await manual.handle(cb)

    async def test_duplicate_phone_blocks_creation_and_offers_force_create(self):
        row = await self._draft_with_phone()
        self.client.find_active_duplicate_lead.return_value = '999'
        await self._select_source(row)
        updated = store.submission(row['id'])
        self.assertEqual(updated['state'], 'duplicate')
        self.assertEqual(updated['duplicate_of'], '999')
        self.client.add_lead.assert_not_awaited()
        self.client.find_active_duplicate_lead.assert_awaited_once_with(['+79991234567'])
        text, keyboard = manual.card(updated)
        self.assertIn('№999', text)
        self.assertIn('forcecreate:', keyboard['inline_keyboard'][0][0]['callback_data'])

    async def test_forcecreate_creates_lead_despite_duplicate(self):
        row = await self._draft_with_phone()
        self.client.find_active_duplicate_lead.return_value = '999'
        await self._select_source(row)
        await manual.handle(self.callback('forcecreate', row, 4))
        updated = store.submission(row['id'])
        self.assertEqual(updated['state'], 'submitted')
        self.client.add_lead.assert_awaited_once()
        fields = self.client.add_lead.call_args.args[0]
        self.assertEqual(fields['PHONE'][0]['VALUE'], '+79991234567')

    async def test_cancel_works_from_duplicate_state(self):
        row = await self._draft_with_phone()
        self.client.find_active_duplicate_lead.return_value = '999'
        await self._select_source(row)
        await manual.handle(self.callback('cancel', row, 5))
        self.assertEqual(store.submission(row['id'])['state'], 'cancelled')

    async def test_no_duplicate_creates_lead_normally_with_phone(self):
        row = await self._draft_with_phone()
        await self._select_source(row)
        self.assertEqual(store.submission(row['id'])['state'], 'submitted')
        self.client.add_lead.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
