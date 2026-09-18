import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import deals, store
from app.bitrix_client import BitrixApiError


class DealTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=SimpleNamespace(
            database_path=self.tmp.name+'/db.sqlite', track_deal_category_id='0',
            director_user_id_set={10,20}, bitrix_webhook_url='https://portal.test/rest/1/x/',
            deal_poll_interval_seconds=15,
        )
        self.client=AsyncMock()
        self.client.get_deal.return_value={'ID':'101','TITLE':'Copied deal','CATEGORY_ID':'0','STAGE_ID':'NEW','SOURCE_ID':'WEB','ASSIGNED_BY_ID':'7','CONTACT_ID':'5'}
        self.client.get_contact.return_value={'NAME':'Иван','PHONE':[{'VALUE':'+79990000000'}]}
        self.client.get_source_name.return_value='TikTok'
        self.client.get_user_name.return_value='Менеджер'
        self.send=AsyncMock(side_effect=[{'message_id':1},{'message_id':2}])
        self.patches=[patch('app.store.get_settings',return_value=self.settings),patch('app.deals.get_settings',return_value=self.settings),
                      patch('app.deals.BitrixClient',return_value=self.client),patch('app.deals.send_telegram_message',new=self.send),
                      patch('app.deals.edit_message_text',new=AsyncMock())]
        for item in self.patches:item.start()
        deals.lock=asyncio.Lock()

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    async def test_tracks_target_pipeline_once_for_each_recipient(self):
        self.assertTrue(await deals.track('101'))
        self.assertTrue(await deals.track('101'))
        self.assertEqual(self.send.await_count,2)
        self.assertEqual(len(deals.deliveries('101')),2)
        text=self.send.call_args_list[0].args[0]
        self.assertIn('Новая сделка',text)
        self.assertIn('+79990000000',text)
        self.assertIn('/crm/deal/details/101/',text)

    async def test_ignores_other_pipeline(self):
        self.client.get_deal.return_value['CATEGORY_ID']='6'
        self.assertFalse(await deals.track('101'))
        self.send.assert_not_awaited()

    async def test_first_poll_sets_cursor_without_backfill(self):
        self.client.get_latest_deal_id.return_value='101'
        self.client.get_new_deals.return_value=[]
        await deals.poll_once()
        self.client.get_new_deals.assert_awaited_once_with('0',101)
        self.send.assert_not_awaited()

    async def test_next_poll_delivers_new_deal(self):
        store.execute("INSERT INTO metadata VALUES (?,?)",(deals.CURSOR_KEY,'100'))
        self.client.get_new_deals.return_value=[{'ID':'101'}]
        await deals.poll_once()
        self.assertEqual(len(deals.deliveries('101')),2)
        self.assertEqual(store.rows('SELECT value FROM metadata')[0]['value'],'101')

    async def test_junk_card_has_status_and_link(self):
        await deals.track('101')
        await deals.refresh_cards(self.client.get_deal.return_value,is_junk=True)
        edit=self.patches[-1].new
        self.assertEqual(edit.await_count,2)
        self.assertIn('Сделка отправлена на стадию «Мусор»',edit.call_args.args[2])
        self.assertIn('/crm/deal/details/101/',edit.call_args.args[2])

    async def test_exact_lead_relation_is_marked_as_previously_seen(self):
        store.record_seen_lead({'ID':'20462','PHONE':[]})
        self.client.get_deal.return_value['LEAD_ID']='20462'
        await deals.track('101')
        text=self.send.call_args_list[0].args[0]
        self.assertIn('Создана из ранее записанного лида',text)
        self.assertIn('/crm/lead/details/20462/',text)

    async def test_phone_relation_fallback_when_copy_lost_lead_id(self):
        store.record_seen_lead({'ID':'20460','PHONE':[{'VALUE':'8 999 000-00-00'}]})
        await deals.track('101')
        text=self.send.call_args_list[0].args[0]
        self.assertIn('Контакт совпадает с ранее записанным лидом',text)
        self.assertIn('/crm/lead/details/20460/',text)


class DealJunkApiTests(unittest.IsolatedAsyncioTestCase):
    def client(self,final='UC_K0Z3P6'):
        from app.bitrix_client import BitrixClient
        client=BitrixClient(webhook_url='https://portal.test/rest/1/x/')
        client.get_deal=AsyncMock(side_effect=[{'ID':'1','CATEGORY_ID':'0','STAGE_ID':'NEW'},{'ID':'1','CATEGORY_ID':'0','STAGE_ID':final}])
        client._call=AsyncMock(return_value=[{'STATUS_ID':'UC_K0Z3P6','NAME':'Мусор'}])
        client.update_deal=AsyncMock()
        return client

    async def test_moves_and_verifies_deal(self):
        client=self.client()
        result=await client.move_deal_to_junk('1')
        client.update_deal.assert_awaited_once_with('1',{'STAGE_ID':'UC_K0Z3P6'})
        self.assertEqual(result['STAGE_ID'],'UC_K0Z3P6')

    async def test_does_not_report_silent_failure(self):
        client=self.client(final='NEW')
        with self.assertRaises(BitrixApiError):
            await client.move_deal_to_junk('1')
