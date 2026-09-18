"""Manual lead entry inside the primary Telegram bot."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from html import escape
from urllib.parse import urlparse

import httpx

from app import store
from app.bitrix_client import BitrixApiError, BitrixClient
from app.config import get_settings
from app.formatter import build_lead_notification, build_manage_keyboard
from app.telegram_client import edit_message_text, send_telegram_message

logger = logging.getLogger(__name__)
lock = asyncio.Lock()
MARKER = "manual-bot:"
ACTIVE_STATES = ("draft", "manager", "comment", "duplicate", "creating", "uncertain")
MAIN_ACTIONS = {"mp", "mt", "ms", "mm", "mskip", "mforce", "mc", "mtrash", "mconfirm", "mkeep"}


def button(text, data):
    return {"text": text, "callback_data": data}


def compact(items, width=2):
    return [items[index:index + width] for index in range(0, len(items), width)]


async def send(chat_id, text, keyboard=None):
    return await send_telegram_message(text, chat_id=chat_id, reply_markup=keyboard)


async def edit(chat_id, message_id, text, keyboard):
    await edit_message_text(chat_id, message_id, text, reply_markup=keyboard)


async def refresh_sources():
    """Synchronize names/IDs; new Bitrix sources start enabled."""
    sources = await BitrixClient().get_sources()
    for source in sources:
        store.execute(
            "INSERT INTO sources VALUES (?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (str(source["STATUS_ID"]), source["NAME"]),
        )
    valid = {str(source["STATUS_ID"]) for source in sources}
    for source in store.rows("SELECT id FROM sources"):
        if source["id"] not in valid:
            store.execute("DELETE FROM sources WHERE id=?", (source["id"],))
    return sources


async def source_menu(chat_id, message_id=None, page=0):
    await refresh_sources()
    sources = store.rows("SELECT * FROM sources ORDER BY name")
    page_size = 12
    page = max(0, min(page, max(0, (len(sources) - 1) // page_size)))
    buttons = [button(("✅ " if source["enabled"] else "▫️ ") + source["name"], f"mt:{source['id']}:{page}")
               for source in sources[page * page_size:(page + 1) * page_size]]
    keyboard = compact(buttons)
    nav = []
    if page:
        nav.append(button("⬅️", f"mp:{page - 1}"))
    if (page + 1) * page_size < len(sources):
        nav.append(button("➡️", f"mp:{page + 1}"))
    if nav:
        keyboard.append(nav)
    text = ("⚙️ <b>Источники ручных заявок</b>\n"
            "Нажмите, чтобы включить или выключить источник. Список и названия синхронизированы с Битриксом.")
    markup = {"inline_keyboard": keyboard}
    if message_id:
        await edit(chat_id, message_id, text, markup)
    else:
        await send(chat_id, text, markup)


def _base_card(row):
    text = f"📋 <b>Ручная заявка</b>\nТелефон: <code>{escape(row['contact'])}</code>"
    if row.get("source_name"):
        text += f"\nИсточник: {escape(row['source_name'])}"
    if row.get("manager"):
        text += f"\nОтветственный: {escape(row['manager'])}"
    if row.get("comment"):
        text += f"\nКомментарий: {escape(row['comment'])}"
    return text


async def card(row):
    text = _base_card(row)
    state = row["state"]
    keyboard = []
    if state == "draft":
        await refresh_sources()
        buttons = [button(source["name"], f"ms:{row['id']}:{source['id']}")
                   for source in store.rows("SELECT * FROM sources WHERE enabled=1 ORDER BY name")]
        keyboard = compact(buttons)
        keyboard.append([button("✖️ Отменить", f"mc:{row['id']}")])
        text += "\n\nВыберите источник:"
    elif state == "manager":
        users = await BitrixClient().get_department_users(get_settings().sales_department_id)
        buttons = [button(
            " ".join(part for part in (user.get("NAME"), user.get("LAST_NAME")) if part) or user.get("EMAIL") or str(user["ID"]),
            f"mm:{row['id']}:{user['ID']}",
        ) for user in users]
        keyboard = compact(buttons)
        keyboard.append([button("✖️ Отменить", f"mc:{row['id']}")])
        text += "\n\nВыберите ответственного:"
    elif state == "comment":
        text += "\n\nОтправьте комментарий следующим сообщением или нажмите «Пропустить»."
        keyboard = [[button("Пропустить", f"mskip:{row['id']}")], [button("✖️ Отменить", f"mc:{row['id']}")]]
    elif state == "duplicate":
        portal = urlparse(get_settings().bitrix_webhook_url).netloc
        text += (f'\n\n⚠️ Активный лид с этим телефоном уже существует: '
                 f'<a href="https://{escape(portal)}/crm/lead/details/{row["duplicate_of"]}/">№{escape(row["duplicate_of"])}</a>')
        keyboard = [[button("Всё равно создать", f"mforce:{row['id']}")], [button("✖️ Отменить", f"mc:{row['id']}")]]
    elif state == "creating":
        text += "\n\n⏳ Создаём лид в Битриксе…"
    elif state == "uncertain":
        text += "\n\n⚠️ Проверяем результат создания в Битриксе. Повторный лид не создаётся."
    elif state == "cancelled":
        text += "\n\n✖️ Передача отменена"
    elif state == "junk":
        text += "\n\n🗑 Отправлен на стадию «Мусор»"
    elif state == "submitted":
        text += f"\n\n✅ Лид №{escape(row['lead_id'] or '')} создан и назначен"
        portal = urlparse(get_settings().bitrix_webhook_url).netloc
        text += f'\n🔗 <a href="https://{escape(portal)}/crm/lead/details/{row["lead_id"]}/">Открыть лид в CRM</a>'
        keyboard = [[button("🗑 В мусор", f"mtrash:{row['id']}")]]
    return text, {"inline_keyboard": keyboard}


async def sync(row):
    text, keyboard = await card(row)
    if row.get("bot_kind") != "main":
        pass
    elif not row["message_id"]:
        result = await send(row["chat_id"], text, keyboard)
        store.save(row["id"], message_id=result["message_id"])
    else:
        await edit(row["chat_id"], row["message_id"], text, keyboard)
    if row["dirty"] and row["lead_id"] and row["state"] == "junk":
        client = BitrixClient()
        lead = await client.get_lead(row["lead_id"])
        assigned_name = await client.get_user_name(str(lead.get("ASSIGNED_BY_ID") or ""))
        notification = build_lead_notification(
            lead, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
            source_name=row["source_name"], assigned_name=assigned_name, is_junk=True,
        )
        for delivery in deliveries(row):
            await edit_message_text(delivery["chat_id"], delivery["message_id"], notification, reply_markup={"inline_keyboard": []})
    store.save(row["id"], dirty=0)


def destinations():
    return [str(user_id) for user_id in get_settings().director_user_id_set]


def deliveries(row):
    if row["main_message_id"]:
        store.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?)",
                      (row["id"], str(get_settings().telegram_chat_id), row["main_message_id"]))
    return store.rows("SELECT * FROM deliveries WHERE submission_id=?", (row["id"],))


async def publish(row):
    if row["state"] != "submitted":
        return
    delivered = {delivery["chat_id"] for delivery in deliveries(row)}
    pending = [chat_id for chat_id in destinations() if chat_id not in delivered]
    if not pending:
        return
    client = BitrixClient()
    lead = await client.get_lead(row["lead_id"])
    store.record_seen_lead(lead)
    text = build_lead_notification(
        lead, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
        source_name=row["source_name"], assigned_name=row["manager"],
    )
    for chat_id in pending:
        result = await send_telegram_message(text, reply_markup=build_manage_keyboard(row["lead_id"]), chat_id=chat_id)
        store.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?)", (row["id"], chat_id, result["message_id"]))
        if chat_id == str(get_settings().telegram_chat_id):
            store.save(row["id"], main_message_id=result["message_id"])


async def _create(row, telegram_user_id):
    store.save(row["id"], state="creating", dirty=1)
    await sync(store.submission(row["id"]))
    comments = f"Контакт: {row['contact']}\nДобавлено вручную (Telegram ID {telegram_user_id})"
    if row.get("comment"):
        comments += f"\n\n{row['comment']}"
    if row.get("duplicate_of"):
        comments += f"\n\nСоздано вручную несмотря на совпадение с активным лидом №{row['duplicate_of']}"
    fields = {
        "TITLE": f"Лид {row['source_name']}", "SOURCE_ID": row["source_id"],
        "SOURCE_DESCRIPTION": MARKER + row["id"], "COMMENTS": comments,
        "ASSIGNED_BY_ID": row["manager_id"],
        "PHONE": [{"VALUE": row["contact"], "VALUE_TYPE": "WORK"}],
    }
    try:
        lead_id = await BitrixClient().add_lead(fields)
    except BitrixApiError:
        store.save(row["id"], state="comment", dirty=1)
        await sync(store.submission(row["id"]))
        await send(row["chat_id"], "Битрикс отклонил создание лида. Попробуйте ещё раз или отмените заявку.")
        return
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError):
        store.save(row["id"], state="uncertain", dirty=1)
        await sync(store.submission(row["id"]))
        return
    store.save(row["id"], state="submitted", lead_id=lead_id, dirty=1)
    row = store.submission(row["id"])
    await publish(row)
    await sync(row)
    manager_chat_id = store.manager_telegram_id(row["manager_id"])
    if manager_chat_id:
        lead = await BitrixClient().get_lead(lead_id)
        text = "📌 <b>На вас назначен лид</b>\n\n" + build_lead_notification(
            lead, portal_domain=urlparse(get_settings().bitrix_webhook_url).netloc,
            source_name=row["source_name"], assigned_name=row["manager"],
        )
        await send_telegram_message(text, chat_id=manager_chat_id)


async def _finish(row, telegram_user_id, *, force=False):
    if not force:
        duplicate_id = await BitrixClient().find_active_duplicate_lead(
            [row["contact"]],
            extra_candidate_ids=store.find_all_seen_lead_ids_by_phones([row["contact"]]),
        )
        if duplicate_id:
            store.save(row["id"], state="duplicate", duplicate_of=duplicate_id, dirty=1)
            await sync(store.submission(row["id"]))
            return
    await _create(row, telegram_user_id)


async def recover():
    async with lock:
        for row in store.rows("SELECT * FROM submissions WHERE state IN ('creating','uncertain')"):
            result = await BitrixClient()._call("crm.lead.list", {
                "filter": {"=SOURCE_DESCRIPTION": MARKER + row["id"]}, "select": ["ID"],
            })
            if result:
                store.save(row["id"], lead_id=str(result[0]["ID"]), state="submitted", dirty=1)
        for row in store.rows("SELECT * FROM submissions WHERE dirty=1 OR state='submitted'"):
            try:
                await publish(row)
                if row["dirty"]:
                    await sync(store.submission(row["id"]))
            except Exception:
                logger.exception("Manual lead recovery failed for %s", row["id"])


async def recovery_loop():
    while True:
        try:
            await recover()
        except Exception:
            logger.exception("Manual lead recovery failed")
        await asyncio.sleep(15)


def _active(chat_id):
    result = store.rows(
        "SELECT * FROM submissions WHERE chat_id=? AND state IN ('draft','manager','comment','duplicate','creating','uncertain') ORDER BY rowid DESC LIMIT 1",
        (chat_id,),
    )
    return result[0] if result else None


async def handle_main_message(message, update_id) -> bool:
    settings = get_settings()
    if message.get("chat", {}).get("type") != "private":
        return False
    user = message.get("from") or {}
    user_id = user.get("id")
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text else ""
    async with lock:
        if command in ("/source", "/sources") and user_id in (settings.director_user_id_set | settings.admin_user_id_set):
            await source_menu(chat_id)
            return True
        if user_id not in settings.director_user_id_set:
            return False
        if command == "/cancel":
            for row in store.rows("SELECT * FROM submissions WHERE chat_id=? AND state IN ('draft','manager','comment','duplicate')", (chat_id,)):
                store.save(row["id"], state="cancelled", dirty=1)
                await sync(store.submission(row["id"]))
            await send(chat_id, "Незавершённые ручные заявки отменены.")
            return True
        active = _active(chat_id)
        if active and active["state"] == "comment" and text and not text.startswith("/"):
            if len(text) > 1500:
                await send(chat_id, "Комментарий должен быть короче 1500 символов.")
                return True
            store.save(active["id"], comment=text, dirty=1)
            await _finish(store.submission(active["id"]), user_id)
            return True
        if active:
            await send(chat_id, "Сначала завершите текущую заявку или используйте /cancel.")
            return True
        if message.get("contact"):
            text = message["contact"].get("phone_number", "")
        if not re.fullmatch(r"\+?[\d\s()\-]{7,25}", text):
            return False
        phone = re.sub(r"[^\d+]", "", text)
        await refresh_sources()
        identifier = uuid.uuid4().hex[:12]
        store.execute("INSERT OR IGNORE INTO submissions (id,update_id,chat_id,contact,bot_kind) VALUES (?,?,?,?,?)",
                      (identifier, update_id, chat_id, phone, "main"))
        await sync(store.submission(identifier))
        return True


async def handle_main_callback(callback_query, update_id) -> bool:
    data = callback_query.get("data", "")
    action = data.split(":", 1)[0]
    if action not in MAIN_ACTIONS:
        return False
    settings = get_settings()
    callback_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    allowed = settings.director_user_id_set | settings.admin_user_id_set if action in {"mp", "mt"} else settings.director_user_id_set
    if user_id not in allowed:
        return False
    message = callback_query["message"]
    chat_id, message_id = message["chat"]["id"], message["message_id"]
    parts = data.split(":")
    async with lock:
        if action == "mp":
            await source_menu(chat_id, message_id, int(parts[1]))
            return True
        if action == "mt":
            with store.db() as conn:
                inserted = conn.execute("INSERT OR IGNORE INTO source_updates VALUES (?)", (update_id,)).rowcount
                if inserted:
                    conn.execute("UPDATE sources SET enabled=1-enabled WHERE id=?", (parts[1],))
            await source_menu(chat_id, message_id, int(parts[2]))
            return True
        row = store.submission(parts[1]) if len(parts) > 1 else None
        if not row or row["chat_id"] != chat_id or row["message_id"] != message_id:
            return True
        if action == "mc" and row["state"] in {"draft", "manager", "comment", "duplicate"}:
            store.save(row["id"], state="cancelled", dirty=1)
        elif action == "ms" and row["state"] == "draft" and len(parts) == 3:
            source = store.rows("SELECT * FROM sources WHERE id=? AND enabled=1", (parts[2],))
            if not source:
                await send(chat_id, "Источник отключён. Выберите другой.")
                return True
            store.save(row["id"], source_id=source[0]["id"], source_name=source[0]["name"], state="manager", dirty=1)
        elif action == "mm" and row["state"] == "manager" and len(parts) == 3:
            users = await BitrixClient().get_department_users(settings.sales_department_id)
            selected = next((user for user in users if str(user["ID"]) == parts[2]), None)
            if not selected:
                await send(chat_id, "Сотрудник больше не входит в отдел продаж.")
                return True
            name = " ".join(part for part in (selected.get("NAME"), selected.get("LAST_NAME")) if part) or selected.get("EMAIL") or str(selected["ID"])
            store.save(row["id"], manager_id=str(selected["ID"]), manager=name, state="comment", dirty=1)
        elif action == "mskip" and row["state"] == "comment":
            await _finish(row, user_id)
            return True
        elif action == "mforce" and row["state"] == "duplicate":
            await _finish(row, user_id, force=True)
            return True
        elif action == "mtrash" and row["state"] == "submitted":
            text, _ = await card(row)
            await edit(chat_id, message_id, text + "\n\nПеренести лид на стадию «Мусор»?", {
                "inline_keyboard": [[button("Да, в мусор", f"mconfirm:{row['id']}")], [button("Оставить", f"mkeep:{row['id']}")]],
            })
            return True
        elif action == "mconfirm" and row["state"] == "submitted":
            await BitrixClient().move_to_junk(row["lead_id"])
            store.save(row["id"], state="junk", dirty=1)
        await sync(store.submission(row["id"]))
        return True
