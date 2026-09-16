import unittest

from app.formatter import (
    build_action_keyboard,
    build_assign_keyboard,
    build_lead_notification,
    build_link_pick_keyboard,
    build_link_target_keyboard,
    build_manage_keyboard,
)


class LeadNotificationTests(unittest.TestCase):
    def test_minimal_lead_only_shows_header(self):
        text = build_lead_notification({'ID': '1'})
        self.assertEqual(text, '🆕 <b>Новый лид</b>')

    def test_escapes_html_in_user_supplied_fields(self):
        lead = {'ID': '1', 'NAME': '<script>alert(1)</script>', 'COMMENTS': 'a & b < c'}
        text = build_lead_notification(lead)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('a &amp; b &lt; c', text)

    def test_empty_multifield_lists_are_skipped(self):
        lead = {'ID': '1', 'PHONE': [], 'EMAIL': [{'VALUE': '', 'VALUE_TYPE': 'WORK'}]}
        text = build_lead_notification(lead)
        self.assertNotIn('📞', text)
        self.assertNotIn('✉️', text)

    def test_phone_and_email_values_are_rendered_as_code(self):
        lead = {'ID': '1', 'PHONE': [{'VALUE': '+79991234567', 'VALUE_TYPE': 'WORK'}],
                'EMAIL': [{'VALUE': 'a@b.com', 'VALUE_TYPE': 'WORK'}]}
        text = build_lead_notification(lead)
        self.assertIn('<code>+79991234567</code>', text)
        self.assertIn('<code>a@b.com</code>', text)

    def test_manual_bot_marker_is_not_shown_as_source_description(self):
        lead = {'ID': '1', 'SOURCE_DESCRIPTION': 'manual-bot:abc123'}
        text = build_lead_notification(lead)
        self.assertNotIn('manual-bot:', text)

    def test_junk_flag_changes_header_and_hides_new_lead_text(self):
        text = build_lead_notification({'ID': '1'}, is_junk=True)
        self.assertIn('Отправлен на стадию «Мусор»', text)
        self.assertNotIn('Новый лид', text)

    def test_crm_link_uses_portal_domain_and_lead_id(self):
        text = build_lead_notification({'ID': '77'}, portal_domain='example.bitrix24.ru')
        self.assertIn('<a href="https://example.bitrix24.ru/crm/lead/details/77/">', text)

    def test_no_crm_link_without_portal_domain(self):
        text = build_lead_notification({'ID': '77'})
        self.assertNotIn('crm/lead/details', text)


class KeyboardTests(unittest.TestCase):
    def test_manage_keyboard_encodes_lead_id(self):
        keyboard = build_manage_keyboard('42')
        self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'], 'm:42')

    def test_action_keyboard_has_assign_junk_and_back(self):
        keyboard = build_action_keyboard('42')
        callback_data = [button['callback_data'] for row in keyboard['inline_keyboard'] for button in row]
        self.assertEqual(callback_data, ['a:42', 'j:42', 'b:42'])

    def test_assign_keyboard_lists_users_and_back_button(self):
        users = [{'ID': '7', 'NAME': 'Иван', 'LAST_NAME': 'Иванов'}, {'ID': '8', 'NAME': None, 'LAST_NAME': None, 'EMAIL': 'x@y.z'}]
        keyboard = build_assign_keyboard('42', users)
        rows = keyboard['inline_keyboard']
        self.assertEqual(rows[0][0]['text'], 'Иван Иванов')
        self.assertEqual(rows[0][0]['callback_data'], 'au:42:7')
        self.assertEqual(rows[1][0]['text'], 'x@y.z')
        self.assertEqual(rows[-1][0]['callback_data'], 'b:42')

    def test_link_pick_keyboard_one_button_per_user(self):
        users = [{'ID': '7', 'NAME': 'Иван', 'LAST_NAME': None}]
        keyboard = build_link_pick_keyboard(users)
        self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'], 'link_pick:7')

    def test_link_target_keyboard_prefers_username_over_first_name(self):
        starts = [{'telegram_id': 555, 'username': 'ivan', 'first_name': 'Иван'}]
        keyboard = build_link_target_keyboard('7', 'Иван Иванов', starts)
        self.assertIn('@ivan', keyboard['inline_keyboard'][0][0]['text'])
        self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'], 'link_to:7:555')
        self.assertEqual(keyboard['inline_keyboard'][-1][0]['callback_data'], 'link_cancel')


if __name__ == '__main__':
    unittest.main()
