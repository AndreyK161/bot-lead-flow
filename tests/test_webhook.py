import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main, manual, store
from app.bitrix_client import LEAD_PREFERRED_CALL_TIME_FIELD, BitrixApiError


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

    async def test_routes_deal_update_to_statistics_without_new_notification(self):
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMDEALUPDATE', 'data[FIELDS][ID]': '19406'})
        with patch('app.main.deals.observe_update', new=AsyncMock(return_value=True)) as observe:
            result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        observe.assert_awaited_once_with('19406')

    async def test_lead_update_refreshes_statistics_without_notification(self):
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADUPDATE', 'data[FIELDS][ID]': '20312'})
        with patch('app.main.stats.observe', new=AsyncMock()) as observe:
            result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        observe.assert_awaited_once_with('lead', self.client.get_lead.return_value, self.client)
        self.send_message.assert_not_awaited()

    async def test_sends_dm_to_every_director(self):
        self.settings.director_user_id_set = {100, 101}
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.assertEqual(self.send_message.await_count, 2)
        chat_ids = {call.kwargs['chat_id'] for call in self.send_message.call_args_list}
        self.assertEqual(chat_ids, {100, 101})

    async def test_adds_preferred_call_time_to_bitrix_comments_and_notification(self):
        self.client.get_lead.return_value = {
            'ID': '20312',
            'NAME': 'Клиент',
            'SOURCE_ID': 'EMAIL',
            'ASSIGNED_BY_ID': '',
            'COMMENTS': 'Заявка с сайта',
            LEAD_PREFERRED_CALL_TIME_FIELD: '17:00 - 17:30',
        }
        request = form_request(**{
            'auth[application_token]': 'apptoken',
            'event': 'ONCRMLEADADD',
            'data[FIELDS][ID]': '20312',
        })

        result = await main.bitrix_webhook(request)

        self.assertEqual(result, {'status': 'ok'})
        expected = 'Заявка с сайта\nЖелаемое время звонка: 17:00 - 17:30'
        self.client.update_lead.assert_awaited_once_with('20312', {'COMMENTS': expected})
        self.assertIn(expected, self.send_message.call_args.args[0])

    async def test_skips_empty_or_already_copied_preferred_call_time(self):
        for lead_id, lead in (
            ('20312', {'ID': '20312', LEAD_PREFERRED_CALL_TIME_FIELD: ''}),
            ('20313', {
                'ID': '20313',
                LEAD_PREFERRED_CALL_TIME_FIELD: 'Сейчас',
                'COMMENTS': 'Желаемое время звонка: Сейчас',
            }),
        ):
            with self.subTest(lead=lead):
                self.client.reset_mock()
                self.client.get_lead.return_value = lead
                request = form_request(**{
                    'auth[application_token]': 'apptoken',
                    'event': 'ONCRMLEADADD',
                    'data[FIELDS][ID]': lead_id,
                })
                await main.bitrix_webhook(request)
                self.client.update_lead.assert_not_awaited()

    async def test_excluded_privileged_user_does_not_receive_new_lead(self):
        self.settings.director_user_id_set = {100, 101}
        self.settings.lead_notification_excluded_user_id_set = {101}
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], 100)

    async def test_excluded_from_general_flow_still_gets_personal_assignment(self):
        manager_chat_id = 8306778724
        self.settings.lead_notification_excluded_user_id_set = {manager_chat_id}
        store.link_manager('460', 'Вячеслав Шандра', manager_chat_id)

        await main._notify_assigned_manager(
            self.client, self.settings, '20312', '460', 'Вячеслав Шандра',
        )

        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], manager_chat_id)
        self.assertIn('На вас назначен лид', self.send_message.call_args.args[0])

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

    async def test_duplicate_lead_is_auto_moved_to_duplicate_stage_and_flagged(self):
        self.client.get_lead.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': '',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        self.client.find_active_duplicate_lead.return_value = '999'
        self.client.move_to_duplicate_stage.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'STATUS_ID': 'UC_DUP',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        call = self.client.find_active_duplicate_lead.call_args
        self.assertEqual(call.args, (['+79991234567'],))
        self.assertEqual(call.kwargs['exclude_lead_id'], '20312')
        self.client.move_to_duplicate_stage.assert_awaited_once_with('20312')
        self.client.move_to_junk.assert_not_awaited()
        text = self.send_message.call_args.args[0]
        self.assertIn('Дубликат', text)
        self.assertIn('Дубль', text)
        self.assertIn('№999', text)

    async def test_non_duplicate_lead_is_not_moved(self):
        self.client.get_lead.return_value = {
            'ID': '20312', 'NAME': 'Клиент', 'SOURCE_ID': 'WEB', 'ASSIGNED_BY_ID': '',
            'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
        }
        self.client.find_active_duplicate_lead.return_value = None
        request = form_request(**{'auth[application_token]': 'apptoken', 'event': 'ONCRMLEADADD', 'data[FIELDS][ID]': '20312'})
        result = await main.bitrix_webhook(request)
        self.assertEqual(result, {'status': 'ok'})
        self.client.move_to_duplicate_stage.assert_not_awaited()
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

    async def test_mode_command_is_private_to_owner(self):
        self.settings.mode_switch_user_id = 1297686797
        for user_id in (1297686797, 555):
            request = json_request(
                headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
                message={'chat': {'id': user_id, 'type': 'private'}, 'from': {'id': user_id}, 'text': '/mode'},
            )
            await main.telegram_webhook(request)
        self.assertEqual(self.send_message.await_count, 1)
        keyboard = self.send_message.call_args.kwargs['reply_markup']
        self.assertEqual([button['callback_data'] for button in keyboard['inline_keyboard'][0]],
                         ['mode:supervisor', 'mode:manager'])

    async def test_owner_can_switch_to_isolated_manager_app(self):
        owner = 1297686797
        self.settings.mode_switch_user_id = owner
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'mode-cb', 'from': {'id': owner}, 'data': 'mode:manager',
                            'message': {'chat': {'id': owner}, 'message_id': 7, 'text': 'mode'}},
        )
        with patch('app.main.set_chat_menu_button', new=AsyncMock()) as set_menu:
            await main.telegram_webhook(request)
        self.assertEqual(store.user_ui_mode(owner), 'manager')
        set_menu.assert_awaited_once_with(
            'https://lead.prav-buro.ru/manager/', chat_id=owner, text='План дня',
        )
        self.assertEqual(
            self.edit_text.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['web_app']['url'],
            'https://lead.prav-buro.ru/manager/',
        )

    async def test_owner_app_keyboard_contains_mode_switch(self):
        owner = 1297686797
        self.settings.mode_switch_user_id = owner
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        keyboard = main._mini_app_keyboard(self.settings, owner)
        self.assertEqual(
            [button['callback_data'] for button in keyboard['inline_keyboard'][1]],
            ['mode:supervisor', 'mode:manager'],
        )

    async def test_period_command_is_sent_only_to_requesting_director(self):
        director_id = 1297686797
        self.settings.director_user_id_set = {director_id}
        message = {
            'chat': {'id': director_id, 'type': 'private'},
            'from': {'id': director_id},
            'text': '/period 01.09.2026 - 23.09.2026',
        }
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, update_id=79, message=message,
        )
        with patch('app.main.period_reports.enqueue_period_report', return_value=True) as enqueue:
            await main.telegram_webhook(request)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs['chat_id'], director_id)
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], director_id)
        self.assertIn('Excel', self.send_message.call_args.args[0])

    async def test_app_command_sends_mini_app_only_to_test_director(self):
        director_id = 1297686797
        self.settings.director_user_id_set = {director_id}
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': director_id, 'type': 'private'},
                'from': {'id': director_id},
                'text': '/app',
            },
        )

        await main.telegram_webhook(request)

        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], director_id)
        button = self.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['web_app']['url'], 'https://lead.prav-buro.ru/miniapp')

    async def test_app_command_is_available_to_admin_without_director_role(self):
        admin_id = 1297686797
        self.settings.director_user_id_set = set()
        self.settings.admin_user_id_set = {admin_id}
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': admin_id, 'type': 'private'},
                'from': {'id': admin_id},
                'text': '/app',
            },
        )

        await main.telegram_webhook(request)

        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.call_args.kwargs['chat_id'], admin_id)
        button = self.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['web_app']['url'], 'https://lead.prav-buro.ru/miniapp')

    async def test_app_command_is_not_available_to_regular_manager(self):
        manager_id = 1297686797
        self.settings.director_user_id_set = set()
        self.settings.admin_user_id_set = set()
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': manager_id, 'type': 'private'},
                'from': {'id': manager_id},
                'text': '/app',
            },
        )

        with patch('app.main.manual.handle_main_message', new=AsyncMock(return_value=False)):
            await main.telegram_webhook(request)

        self.send_message.assert_not_awaited()

    async def test_enabled_planner_manager_is_routed_only_to_manager_app(self):
        manager_id = 7939114214
        self.settings.director_user_id_set = set()
        self.settings.admin_user_id_set = set()
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        store.upsert_planner_access(
            manager_id, bitrix_user_id='110', display_name='Яна', role='manager',
            active=True, source='test',
        )
        store.set_planner_login_enabled(manager_id, True)
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': manager_id, 'type': 'private'},
                'from': {'id': manager_id}, 'text': '/app',
            },
        )

        await main.telegram_webhook(request)

        button = self.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['web_app']['url'], 'https://lead.prav-buro.ru/manager/')
        self.assertIn('личный план', self.send_message.call_args.args[0])

    async def test_privileged_manager_gets_personal_plan_and_reports(self):
        manager_id = 8306778724
        self.settings.director_user_id_set = {manager_id}
        self.settings.admin_user_id_set = {manager_id}
        self.settings.super_admin_user_id_set = {manager_id}
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        store.upsert_planner_access(
            manager_id, bitrix_user_id='460', display_name='Вячеслав', role='manager',
            active=True, source='test',
        )
        store.set_planner_login_enabled(manager_id, True)
        keyboard = main._mini_app_keyboard(self.settings, manager_id)
        buttons = keyboard['inline_keyboard']
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0][0]['web_app']['url'], 'https://lead.prav-buro.ru/manager/')
        self.assertEqual(buttons[0][0]['text'], '🗓 Открыть план дня')

    async def test_enabled_supervisor_gets_planner_and_reports(self):
        supervisor_id = 1073597301
        self.settings.director_user_id_set = set()
        self.settings.admin_user_id_set = set()
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        self.settings.mini_app_url = 'https://lead.prav-buro.ru/miniapp'
        store.upsert_planner_access(
            supervisor_id, bitrix_user_id=None, display_name='Руководитель', role='supervisor',
            active=True, source='test',
        )
        store.set_planner_login_enabled(supervisor_id, True)
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': supervisor_id, 'type': 'private'},
                'from': {'id': supervisor_id}, 'text': '/app',
            },
        )
        await main.telegram_webhook(request)
        buttons = self.send_message.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(
            [row[0]['web_app']['url'] for row in buttons],
            ['https://lead.prav-buro.ru/manager/', 'https://lead.prav-buro.ru/miniapp'],
        )
        self.assertIn('планы сотрудников', self.send_message.call_args.args[0])

    async def test_enabled_planner_user_can_start_distribution_after_cutover(self):
        manager_id = 7939114214
        store.upsert_planner_access(
            manager_id, bitrix_user_id='110', display_name='Яна', role='manager',
            active=True, source='test',
        )
        store.set_planner_login_enabled(manager_id, True)
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': manager_id, 'type': 'private'},
                'from': {'id': manager_id}, 'text': '/distribution',
            },
        )
        with patch('app.main.distribution_flow.start', new=AsyncMock()) as start:
            await main.telegram_webhook(request)
        start.assert_awaited_once_with(manager_id, manager_id, self.settings)

    async def test_owner_can_enable_active_planner_manager(self):
        owner_id = 1297686797
        manager_id = 7939114214
        self.settings.mode_switch_user_id = owner_id
        self.settings.manager_app_url = 'https://lead.prav-buro.ru/manager/'
        store.upsert_planner_access(
            manager_id, bitrix_user_id='110', display_name='Яна', role='manager',
            active=True, source='test',
        )
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': owner_id, 'type': 'private'},
                'from': {'id': owner_id}, 'text': f'/planner_enable {manager_id}',
            },
        )
        with patch('app.main.set_chat_menu_button', new=AsyncMock()) as set_menu:
            await main.telegram_webhook(request)
        self.assertTrue(store.planner_access(manager_id)['login_enabled'])
        set_menu.assert_awaited_once_with(
            'https://lead.prav-buro.ru/manager/', chat_id=manager_id, text='План дня',
        )

    async def test_owner_cannot_enable_inactive_planner_identity(self):
        owner_id = 1297686797
        inactive_id = 399005637
        self.settings.mode_switch_user_id = owner_id
        store.upsert_planner_access(
            inactive_id, bitrix_user_id=None, display_name='Тестовый', role='supervisor',
            active=False, source='test',
        )
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            message={
                'chat': {'id': owner_id, 'type': 'private'},
                'from': {'id': owner_id}, 'text': f'/planner_enable {inactive_id}',
            },
        )
        await main.telegram_webhook(request)
        self.assertFalse(store.planner_access(inactive_id)['login_enabled'])
        self.assertIn('неактивна', self.send_message.call_args.args[0])

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
        store.link_manager('460', 'Никита Продажников', 556)
        request = json_request(
            headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'},
            callback_query={'id': 'cb1', 'from': {'id': 100}, 'data': 'au:20312:460',
                            'message': {'chat': {'id': 100}, 'message_id': 10, 'text': 'card text'}},
        )
        await main.telegram_webhook(request)
        self.client.update_lead.assert_awaited_once_with('20312', {'ASSIGNED_BY_ID': '460'})
        chat_ids = [call.kwargs.get('chat_id') for call in self.send_message.call_args_list]
        self.assertIn(555, chat_ids)
        self.assertIn(556, chat_ids)

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
