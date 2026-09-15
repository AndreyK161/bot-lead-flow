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
        self.settings = SimpleNamespace(database_path=self.tmp.name+'/db.sqlite', admin_telegram_user_id_set={123},
            manual_bot_token='test', manual_webhook_secret='secret', telegram_chat_id='-100',
            bitrix_webhook_url='https://example.com/rest/1/test/', sales_department_id='5')
        self.patches = [patch('app.store.get_settings', return_value=self.settings),
                        patch('app.manual.get_settings', return_value=self.settings),
                        patch('app.main.get_settings', return_value=self.settings)]
        for p in self.patches: p.start()
        self.client = AsyncMock()
        self.client.get_sources.return_value = [{'STATUS_ID': 'vk', 'NAME': 'Вконтакте'}, {'STATUS_ID': 'max', 'NAME': 'Max Станислава'}, {'STATUS_ID': 'yandex', 'NAME': 'Яндекс'}]
        self.client.add_lead.return_value = '42'
        self.client.get_lead.return_value = {'ID': '42', 'SOURCE_ID': 'vk', 'SOURCE_DESCRIPTION': manual.MARKER+'test'}
        self.client.get_department_users.return_value = [{'ID': '7', 'NAME': 'Иван'}]
        self.client.get_user_name.return_value = 'Иван'
        self.client.get_source_name.return_value = 'Вконтакте'
        self.patches += [patch('app.manual.BitrixClient', return_value=self.client), patch('app.main.BitrixClient', return_value=self.client),
                         patch('app.manual.call', new=AsyncMock(return_value={'result': {'message_id': 10}})),
                         patch('app.manual.send_telegram_message', new=AsyncMock(return_value={'message_id': 99})),
                         patch('app.manual.edit_message_text', new=AsyncMock()),
                         patch('app.main.edit_message_text', new=AsyncMock()), patch('app.main.answer_callback_query', new=AsyncMock())]
        for p in self.patches[3:]: p.start()
        manual.lock = asyncio.Lock()

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

    async def test_delete_requires_second_press_and_is_idempotent(self):
        row = await self.submitted()
        await manual.handle(self.callback('delete',row))
        self.client.delete_lead.assert_not_awaited()
        await manual.handle(self.callback('confirm',row,3))
        await manual.handle(self.callback('confirm',row,3))
        self.client.delete_lead.assert_awaited_once_with('42')
        self.assertEqual(store.submission(row['id'])['state'],'deleted')
        manual.edit_message_text.assert_awaited_once()

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


if __name__ == '__main__':
    unittest.main()
