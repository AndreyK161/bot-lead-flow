import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app import manual, store
from app.bitrix_client import BitrixApiError


class ManualMainBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=SimpleNamespace(
            database_path=self.tmp.name+'/db.sqlite', director_user_id_set={123},
            admin_user_id_set={200},
            telegram_chat_id='-100', bitrix_webhook_url='https://portal.test/rest/1/x/',
            sales_department_id='5',
        )
        self.client=AsyncMock()
        self.client.get_sources.return_value=[
            {'STATUS_ID':'vk','NAME':'Вконтакте'}, {'STATUS_ID':'tg','NAME':'Телеграм'},
            {'STATUS_ID':'other','NAME':'Иное'},
        ]
        self.client.get_department_users.return_value=[
            {'ID':'7','NAME':'Иван','LAST_NAME':'Иванов'}, {'ID':'8','NAME':'Анна','LAST_NAME':'Петрова'},
        ]
        self.client.add_lead.return_value='42'
        self.client.get_lead.return_value={'ID':'42','PHONE':[{'VALUE':'+79990000000'}],'SOURCE_ID':'tg','ASSIGNED_BY_ID':'7'}
        self.client.get_user_name.return_value='Иван Иванов'
        self.client.find_active_duplicate_lead.return_value=None
        self.send=AsyncMock(side_effect=lambda text,**kwargs:{'message_id':100})
        self.edit=AsyncMock()
        self.patches=[
            patch('app.store.get_settings',return_value=self.settings), patch('app.manual.get_settings',return_value=self.settings),
            patch('app.manual.BitrixClient',return_value=self.client), patch('app.manual.send_telegram_message',new=self.send),
            patch('app.manual.edit_message_text',new=self.edit),
        ]
        for item in self.patches:item.start()
        manual.lock=asyncio.Lock()

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def message(self,text,uid=1,user=123):
        return {'chat':{'id':user,'type':'private'},'from':{'id':user},'text':text}

    def callback(self,data,message_id=100,user=123):
        return {'id':'cb','from':{'id':user},'data':data,'message':{'chat':{'id':user,'type':'private'},'message_id':message_id}}

    async def draft(self):
        self.assertTrue(await manual.handle_main_message(self.message('+7 999 000-00-00'),1))
        return store.rows('SELECT * FROM submissions')[0]

    async def reach_comment(self):
        row=await self.draft()
        await manual.handle_main_callback(self.callback(f'ms:{row["id"]}:tg'),2)
        await manual.handle_main_callback(self.callback(f'mm:{row["id"]}:7'),3)
        return store.submission(row['id'])

    async def test_all_bitrix_sources_start_enabled_and_layout_is_compact(self):
        await manual.source_menu(123)
        self.assertEqual(len(store.rows('SELECT * FROM sources WHERE enabled=1')),3)
        keyboard=self.send.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(len(keyboard[0]),2)

    async def test_source_command_toggles_persist_without_resync_reset(self):
        await manual.handle_main_message(self.message('/source'),1)
        await manual.handle_main_callback(self.callback('mt:vk:0'),2)
        await manual.refresh_sources()
        self.assertEqual(store.rows('SELECT enabled FROM sources WHERE id=?',('vk',))[0]['enabled'],0)

    async def test_admin_can_manage_sources_but_cannot_create_lead(self):
        self.assertTrue(await manual.handle_main_message(self.message('/source',user=200),1))
        self.assertFalse(await manual.handle_main_message(self.message('+79990000000',user=200),2))
        self.assertFalse(store.rows('SELECT * FROM submissions'))

    async def test_flow_waits_for_source_manager_and_comment_before_creation(self):
        row=await self.draft()
        self.client.add_lead.assert_not_awaited()
        await manual.handle_main_callback(self.callback(f'ms:{row["id"]}:tg'),2)
        self.assertEqual(store.submission(row['id'])['state'],'manager')
        self.client.get_department_users.assert_awaited()
        await manual.handle_main_callback(self.callback(f'mm:{row["id"]}:7'),3)
        self.assertEqual(store.submission(row['id'])['state'],'comment')
        self.client.add_lead.assert_not_awaited()
        await manual.handle_main_message(self.message('Позвонить после 18:00'),4)
        saved=store.submission(row['id'])
        self.assertEqual(saved['state'],'submitted')
        fields=self.client.add_lead.call_args.args[0]
        self.assertEqual(fields['SOURCE_ID'],'tg')
        self.assertEqual(fields['ASSIGNED_BY_ID'],'7')
        self.assertIn('Позвонить после 18:00',fields['COMMENTS'])
        self.assertEqual(fields['TITLE'],'Лид Телеграм')

    async def test_comment_can_be_skipped(self):
        row=await self.reach_comment()
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        fields=self.client.add_lead.call_args.args[0]
        self.assertNotIn('None',fields['COMMENTS'])
        self.assertEqual(store.submission(row['id'])['state'],'submitted')

    async def test_managers_are_loaded_and_stale_manager_is_rejected(self):
        row=await self.draft()
        await manual.handle_main_callback(self.callback(f'ms:{row["id"]}:tg'),2)
        self.client.get_department_users.return_value=[]
        await manual.handle_main_callback(self.callback(f'mm:{row["id"]}:7'),3)
        self.assertEqual(store.submission(row['id'])['state'],'manager')
        self.client.add_lead.assert_not_awaited()

    async def test_cancel_before_creation(self):
        row=await self.draft()
        await manual.handle_main_callback(self.callback(f'mc:{row["id"]}'),2)
        self.assertEqual(store.submission(row['id'])['state'],'cancelled')
        self.client.add_lead.assert_not_awaited()

    async def test_unauthorized_user_is_not_handled(self):
        self.assertFalse(await manual.handle_main_message(self.message('+79990000000',user=999),1))
        self.assertFalse(store.rows('SELECT * FROM submissions'))

    async def test_duplicate_update_does_not_duplicate_draft(self):
        await self.draft()
        await manual.handle_main_message(self.message('+79990000000'),1)
        self.assertEqual(len(store.rows('SELECT * FROM submissions')),1)

    async def test_timeout_marks_uncertain_and_recovery_does_not_create_again(self):
        row=await self.reach_comment()
        self.client.add_lead.side_effect=httpx.ReadTimeout('timeout')
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        self.assertEqual(store.submission(row['id'])['state'],'uncertain')
        self.client._call.return_value=[{'ID':'42'}]
        self.client.add_lead.side_effect=None
        await manual.recover()
        self.assertEqual(store.submission(row['id'])['state'],'submitted')
        self.client.add_lead.assert_awaited_once()

    async def test_bitrix_rejection_returns_to_comment_step(self):
        row=await self.reach_comment()
        self.client.add_lead.side_effect=BitrixApiError('denied')
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        self.assertEqual(store.submission(row['id'])['state'],'comment')

    async def test_junk_preserves_lead(self):
        row=await self.reach_comment()
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        self.client.move_to_junk.return_value=self.client.get_lead.return_value
        await manual.handle_main_callback(self.callback(f'mtrash:{row["id"]}'),5)
        await manual.handle_main_callback(self.callback(f'mconfirm:{row["id"]}'),6)
        self.client.move_to_junk.assert_awaited_once_with('42')
        self.assertEqual(store.submission(row['id'])['state'],'junk')

    async def test_duplicate_is_shown_after_optional_comment_step(self):
        row=await self.reach_comment()
        self.client.find_active_duplicate_lead.return_value='999'
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        saved=store.submission(row['id'])
        self.assertEqual(saved['state'],'duplicate')
        self.assertEqual(saved['duplicate_of'],'999')
        self.client.add_lead.assert_not_awaited()
        text,keyboard=await manual.card(saved)
        self.assertIn('№999',text)
        self.assertIn('mforce:',keyboard['inline_keyboard'][0][0]['callback_data'])

    async def test_duplicate_can_be_force_created_with_assignment(self):
        row=await self.reach_comment()
        self.client.find_active_duplicate_lead.return_value='999'
        await manual.handle_main_callback(self.callback(f'mskip:{row["id"]}'),4)
        await manual.handle_main_callback(self.callback(f'mforce:{row["id"]}'),5)
        self.assertEqual(store.submission(row['id'])['state'],'submitted')
        fields=self.client.add_lead.call_args.args[0]
        self.assertEqual(fields['ASSIGNED_BY_ID'],'7')
        self.assertIn('№999',fields['COMMENTS'])


if __name__=='__main__':unittest.main()
