"""Admin-only bot for manual lead entry, source selection and deletion."""
import asyncio
import logging
import re
import uuid
from html import escape
from urllib.parse import urlparse

import httpx

from app import store
from app.bitrix_client import BitrixClient, BitrixApiError
from app.config import get_settings
from app.formatter import build_lead_notification, build_manage_keyboard
from app.telegram_client import _call, send_telegram_message, edit_message_text

logger = logging.getLogger(__name__)
lock = asyncio.Lock()
MARKER = 'manual-bot:'
SOCIAL = re.compile(r'тик\s*ток|вацап|whats\s*app|инстаграм|instagram|telegram|телеграм|tgapi|вконтакте|одноклассники|ютуб|youtube|\bmax\b|\bмакс\b', re.I)


async def call(method, payload):
    return await _call(method, payload, manual=True)


async def send(chat_id, text, keyboard=None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if keyboard is not None:
        payload['reply_markup'] = keyboard
    return (await call('sendMessage', payload))['result']


async def edit(chat_id, message_id, text, keyboard):
    try:
        await call('editMessageText', {'chat_id': chat_id, 'message_id': message_id, 'text': text,
                                     'parse_mode': 'HTML', 'reply_markup': keyboard, 'disable_web_page_preview': True})
    except Exception as exc:
        if 'message is not modified' not in str(exc):
            raise


async def refresh_sources():
    sources = await BitrixClient().get_sources()
    for source in sources:
        sid, name = str(source['STATUS_ID']), source['NAME']
        store.execute('INSERT INTO sources VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',
                      (sid, name, int(bool(SOCIAL.search(name)))))
    # Disabled entries stay disabled on refresh. Removed CRM sources cannot be selected.
    valid = {str(source['STATUS_ID']) for source in sources}
    for source in store.rows('SELECT id FROM sources'):
        if source['id'] not in valid:
            store.execute('DELETE FROM sources WHERE id=?', (source['id'],))


def button(text, data):
    return {'text': text, 'callback_data': data}


async def source_menu(chat_id, message_id=None, page=0):
    await refresh_sources()
    sources = store.rows('SELECT * FROM sources ORDER BY name')
    page = max(0, min(page, max(0, (len(sources)-1)//10)))
    keyboard = [[button(('✅ ' if s['enabled'] else '▫️ ') + s['name'], f'st:{s["id"]}:{page}')] for s in sources[page*10:(page+1)*10]]
    nav = []
    if page:
        nav.append(button('⬅️', f'sp:{page-1}'))
    if (page+1)*10 < len(sources):
        nav.append(button('➡️', f'sp:{page+1}'))
    if nav:
        keyboard.append(nav)
    text = '⚙️ <b>Источники ручных заявок</b>\nНажмите на источник, чтобы включить или выключить его. Изменения сохраняются сразу.\nСписок загружается из Битрикса; названия и ID совпадают с CRM.\n\nДля новой заявки отправьте телефон или username.'
    markup = {'inline_keyboard': keyboard}
    if message_id:
        await edit(chat_id, message_id, text, markup)
    else:
        await send(chat_id, text, markup)


def card(row):
    text = f'📋 <b>Ручная заявка</b>\nКонтакт: <code>{escape(row["contact"])}</code>'
    if row['source_name']:
        text += f'\nИсточник: {escape(row["source_name"])}'
    state = row['state']
    keyboard = []
    if state == 'draft':
        text += '\n\nВыберите источник:'
        keyboard = [[button(s['name'], f'select:{row["id"]}:{s["id"]}')] for s in store.rows('SELECT * FROM sources WHERE enabled=1 ORDER BY name')]
        keyboard.append([button('✖️ Отменить передачу', f'cancel:{row["id"]}')])
    elif state == 'creating':
        text += '\n⏳ Создаём лид в Битриксе…'
    elif state == 'uncertain':
        text += '\n⚠️ Не удалось подтвердить создание. Проверяем Битрикс, повторно лид не создаётся.'
    elif state == 'cancelled':
        text += '\n✖️ Передача отменена'
    elif state == 'deleted':
        text += '\n🗑 Лид удалён из Битрикса'
    elif state == 'junk':
        text += '\n🗑 Отправлен на стадию «Мусор»'
        portal = urlparse(get_settings().bitrix_webhook_url).netloc
        text += f'\n🔗 <a href="https://{escape(portal)}/crm/lead/details/{row["lead_id"]}/">Открыть лид в CRM</a>'
    else:
        text += f'\nЛид №{escape(row["lead_id"] or "")}\n'
        text += f'✅ Назначен менеджер: {escape(row["manager"])}' if row['manager'] else '⏳ Ожидает назначения менеджера'
        keyboard = [[button('🗑 В мусор', f'delete:{row["id"]}')]]
    return text, {'inline_keyboard': keyboard}


async def sync(row):
    if not row['message_id']:
        result = await send(row['chat_id'], *card(row))
        store.save(row['id'], message_id=result['message_id'])
    else:
        await edit(row['chat_id'], row['message_id'], *card(row))
    if row['dirty'] and row['lead_id'] and row['main_message_id'] and row['state'] == 'deleted':
        for delivery in deliveries(row):
            await edit_message_text(delivery['chat_id'], delivery['message_id'],
                                    f'🗑 Ручной лид №{row["lead_id"]} удалён\nКонтакт: {escape(row["contact"])}', reply_markup={'inline_keyboard': []})
    if row['dirty'] and row['lead_id'] and row['state'] == 'junk':
        client = BitrixClient()
        lead = await client.get_lead(row['lead_id'])
        assigned_name = await client.get_user_name(str(lead.get('ASSIGNED_BY_ID') or ''))
        text = build_lead_notification(lead, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
                                       source_name=row['source_name'], assigned_name=assigned_name, is_junk=True)
        for delivery in deliveries(row):
            await edit_message_text(delivery['chat_id'], delivery['message_id'], text, reply_markup=build_manage_keyboard(row['lead_id']))
    store.save(row['id'], dirty=0)


def destinations():
    settings = get_settings()
    return getattr(settings, 'notification_chat_ids', [settings.telegram_chat_id])


def deliveries(row):
    # Migrate primary cards created before multiple recipients were supported.
    if row['main_message_id']:
        store.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?)',
                      (row['id'], str(get_settings().telegram_chat_id), row['main_message_id']))
    return store.rows('SELECT * FROM deliveries WHERE submission_id=?', (row['id'],))


async def publish(row):
    if row['state'] != 'submitted':
        return
    delivered = {delivery['chat_id'] for delivery in deliveries(row)}
    pending = [str(chat_id) for chat_id in destinations() if str(chat_id) not in delivered]
    if not pending:
        return
    client = BitrixClient()
    lead = await client.get_lead(row['lead_id'])
    text = build_lead_notification(lead, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
                                   source_name=row['source_name'])
    for chat_id in pending:
        result = await send_telegram_message(text, reply_markup=build_manage_keyboard(row['lead_id']), chat_id=chat_id)
        store.execute('INSERT INTO deliveries VALUES (?,?,?)', (row['id'], chat_id, result['message_id']))
        if chat_id == str(get_settings().telegram_chat_id):
            store.save(row['id'], main_message_id=result['message_id'])


async def recover():
    async with lock:
        for row in store.rows("SELECT * FROM submissions WHERE state IN ('creating','uncertain')"):
            result = await BitrixClient()._call('crm.lead.list', {'filter': {'=SOURCE_DESCRIPTION': MARKER + row['id']}, 'select': ['ID']})
            if result:
                store.save(row['id'], lead_id=str(result[0]['ID']), state='submitted', dirty=1)
            elif row['state'] == 'creating':
                store.save(row['id'], state='uncertain', dirty=1)
        for row in store.rows("SELECT * FROM submissions WHERE dirty=1 OR state='submitted'"):
            try:
                await publish(row)
                if row['dirty']:
                    await sync(store.submission(row['id']))
            except Exception:
                logger.error('Manual card recovery failed for %s', row['id'])


async def recovery_loop():
    while True:
        try:
            await recover()
        except Exception:
            logger.error('Manual recovery failed')
        await asyncio.sleep(15)


async def handle(update):
    settings = get_settings()
    cb = update.get('callback_query')
    message = cb.get('message') if cb else update.get('message')
    user = cb.get('from') if cb else (message or {}).get('from')
    if not message or not user:
        return
    chat_id = message['chat']['id']
    if user['id'] not in settings.director_user_id_set or message['chat']['type'] != 'private':
        if cb:
            await call('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Нет доступа', 'show_alert': True})
        else:
            await send(chat_id, 'Нет доступа. Бот доступен администраторам в личных сообщениях.')
        return
    async with lock:
        if cb:
            await call('answerCallbackQuery', {'callback_query_id': cb['id']})
            parts = cb.get('data', '').split(':')
            action = parts[0]
            if action == 'sp':
                await source_menu(chat_id, message['message_id'], int(parts[1]))
                return
            if action == 'st':
                with store.db() as conn:
                    inserted = conn.execute('INSERT OR IGNORE INTO source_updates VALUES (?)', (update['update_id'],)).rowcount
                    if inserted:
                        conn.execute('UPDATE sources SET enabled=1-enabled WHERE id=?', (parts[1],))
                await source_menu(chat_id, message['message_id'], int(parts[2]))
                return
            if len(parts) < 2:
                return
            row = store.submission(parts[1])
            if not row or row['chat_id'] != chat_id or row['message_id'] != message['message_id']:
                return
            if action == 'cancel' and row['state'] == 'draft':
                store.save(row['id'], state='cancelled', dirty=1)
            elif action == 'select' and row['state'] == 'draft' and len(parts) == 3:
                sources = store.rows('SELECT * FROM sources WHERE id=? AND enabled=1', (parts[2],))
                if not sources:
                    await send(chat_id, 'Источник отключён. Выберите другой источник.')
                    return
                source = sources[0]
                store.save(row['id'], state='creating', source_id=source['id'], source_name=source['name'], dirty=1)
                fields = {'TITLE': f'Лид {source["name"]}', 'SOURCE_ID': source['id'],
                          'SOURCE_DESCRIPTION': MARKER + row['id'], 'COMMENTS': f'Контакт: {row["contact"]}\nДобавлено вручную (Telegram ID {user["id"]})'}
                if re.fullmatch(r'\+?[\d\s()\-]{7,25}', row['contact']):
                    fields['PHONE'] = [{'VALUE': re.sub(r'[^\d+]', '', row['contact']), 'VALUE_TYPE': 'WORK'}]
                try:
                    lead_id = await BitrixClient().add_lead(fields)
                except BitrixApiError:
                    store.save(row['id'], state='draft', dirty=1)
                    await sync(store.submission(row['id']))
                    await send(chat_id, 'Битрикс отклонил создание лида. Попробуйте снова или отмените передачу.')
                    return
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError):
                    store.save(row['id'], state='uncertain', dirty=1)
                    await sync(store.submission(row['id']))
                    return
                store.save(row['id'], state='submitted', lead_id=lead_id, dirty=1)
                await publish(store.submission(row['id']))
            elif action == 'delete' and row['state'] == 'submitted':
                text, _ = card(row)
                await edit(chat_id, row['message_id'], text + '\n\nПеренести этот лид на стадию «Мусор»?', {'inline_keyboard': [[button('Да, в мусор', f'confirm:{row["id"]}')], [button('Оставить лид', f'keep:{row["id"]}')]]})
                return
            elif action == 'confirm' and row['state'] == 'submitted':
                try:
                    await BitrixClient().move_to_junk(row['lead_id'])
                except BitrixApiError as exc:
                    await send(chat_id, f'Не удалось перенести лид: {escape(str(exc))}')
                    await sync(row)
                    return
                store.save(row['id'], state='junk', dirty=1)
            await sync(store.submission(row['id']))
            return
        text = (message.get('text') or '').strip()
        command = text.split()[0].split('@')[0] if text else ''
        if command in ('/start', '/help'):
            await send(chat_id, 'Отправьте номер телефона, @username или ссылку на профиль одним сообщением. Затем выберите источник — лид появится в Битриксе и основном боте.\n\n/sources — включить или выключить источники.\n/cancel — отменить все заявки, для которых ещё не выбран источник.')
        elif command == '/sources':
            await source_menu(chat_id)
        elif command == '/cancel':
            for row in store.rows("SELECT * FROM submissions WHERE chat_id=? AND state='draft'", (chat_id,)):
                store.save(row['id'], state='cancelled', dirty=1)
                await sync(store.submission(row['id']))
            await send(chat_id, 'Заявки на стадии выбора источника отменены.')
        elif text or message.get('contact'):
            if message.get('contact'):
                text = message['contact']['phone_number']
            if text.startswith('/') or len(text) > 200 or '\n' in text or len(text) < 3:
                await send(chat_id, 'Отправьте один телефон, username или ссылку на профиль (до 200 символов).')
                return
            existing = store.rows('SELECT * FROM submissions WHERE update_id=?', (update['update_id'],))
            if existing:
                if not existing[0]['message_id']:
                    await sync(existing[0])
                return
            await refresh_sources()
            if not store.rows('SELECT id FROM sources WHERE enabled=1'):
                await send(chat_id, 'Нет включённых источников. Настройте их командой /sources.')
                return
            identifier = uuid.uuid4().hex[:12]
            store.execute('INSERT INTO submissions (id,update_id,chat_id,contact) VALUES (?,?,?,?)', (identifier, update['update_id'], chat_id, text))
            await sync(store.submission(identifier))
