"""FastAPI service for Bitrix24 leads/deals, Telegram notifications and management."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status

from app import deals, journal, manual, reconcile, reports, stats, store
from app.bitrix_client import BitrixApiError, BitrixClient
from app.config import get_settings
from app.formatter import (
    build_action_keyboard,
    build_assign_keyboard,
    build_deal_action_keyboard,
    build_deal_assign_keyboard,
    build_deal_manage_keyboard,
    build_lead_notification,
    build_link_pick_keyboard,
    build_link_target_keyboard,
    build_manage_keyboard,
)
from app.telegram_client import (
    answer_callback_query,
    edit_message_reply_markup,
    edit_message_text,
    send_telegram_message,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot-lead-flow")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app):
    tasks = [
        asyncio.create_task(deals.polling_loop()),
        asyncio.create_task(manual.recovery_loop()),
        asyncio.create_task(reports.daily_report_loop()),
        asyncio.create_task(reconcile.reconciliation_loop()),
        asyncio.create_task(_bitrix_event_recovery_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="bot-lead-flow", lifespan=lifespan)


def _portal_domain(webhook_url: str) -> str | None:
    """Достаёт домен портала из адреса входящего вебхука, чтобы собрать ссылку на лид."""
    host = urlparse(webhook_url).netloc
    return host or None


async def _process_bitrix_event(event: str, entity_id: str | None) -> dict[str, str]:
    settings = get_settings()
    if event in {"ONCRMDEALADD", "ONCRMDEALUPDATE"}:
        if not entity_id:
            raise HTTPException(status_code=400, detail="Missing deal id")
        async with deals.lock:
            tracked = await (
                deals.track(entity_id)
                if event == "ONCRMDEALADD"
                else deals.observe_update(entity_id)
            )
        return {"status": "ok" if tracked else "ignored"}

    if event not in {"ONCRMLEADADD", "ONCRMLEADUPDATE"}:
        # Не наш эндпоинт настроен на другое событие — просто игнорируем.
        return {"status": "ignored"}

    if not entity_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing lead id")
    lead_id = entity_id

    client = BitrixClient()

    try:
        lead = await client.get_lead(lead_id)
        store.record_seen_lead(lead)
        await stats.observe("lead", lead, client)

        if event == "ONCRMLEADUPDATE":
            return {"status": "ok"}

        # "Ручной" бот публикует уведомление сам после создания лида — здесь его дублировать не надо.
        if str(lead.get("SOURCE_DESCRIPTION", "")).startswith(manual.MARKER):
            return {"status": "manual"}

        source_name = None
        if lead.get("SOURCE_ID"):
            source_name = await client.get_source_name(lead["SOURCE_ID"])

        assigned_name = None
        if lead.get("ASSIGNED_BY_ID"):
            assigned_name = await client.get_user_name(lead["ASSIGNED_BY_ID"])

        duplicate_of_lead_id = None
        phones = [(item or {}).get("VALUE") for item in (lead.get("PHONE") or [])]
        if phones:
            duplicate_of_lead_id = await client.find_active_duplicate_lead(
                phones,
                exclude_lead_id=lead_id,
                extra_candidate_ids=store.find_all_seen_lead_ids_by_phones(phones),
            )
    except BitrixApiError:
        logger.exception("Bitrix API call failed for lead_id=%s", lead_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bitrix API error")

    if duplicate_of_lead_id:
        try:
            lead = await client.move_to_duplicate_stage(lead_id)
            await stats.observe("lead", lead, client)
        except BitrixApiError:
            logger.exception("Failed to auto-move duplicate lead_id=%s to «Дубль»", lead_id)

    message = build_lead_notification(
        lead,
        portal_domain=_portal_domain(settings.bitrix_webhook_url),
        source_name=source_name,
        assigned_name=assigned_name,
        duplicate_of_lead_id=duplicate_of_lead_id,
    )

    # Каждому руководителю в личку — уведомление с кнопками управления. Общего чата нет.
    keyboard = build_manage_keyboard(lead_id)
    for admin_id in settings.director_user_id_set:
        try:
            await send_telegram_message(message, chat_id=admin_id, reply_markup=keyboard)
        except Exception:
            # Например, руководитель ещё ни разу не писал боту в личку — бот не может начать диалог первым.
            logger.exception("Failed to send Telegram DM to admin_id=%s for lead_id=%s", admin_id, lead_id)

    return {"status": "ok"}


@app.post("/bitrix/webhook")
async def bitrix_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()

    # Bitrix шлёт form-urlencoded с вложенными ключами вида data[FIELDS][ID].
    form = await request.form()
    application_token = form.get("auth[application_token]")
    allowed_tokens = getattr(settings, "bitrix_application_token_set", {settings.bitrix_application_token})
    if application_token not in allowed_tokens:
        raw_token = str(application_token or "")
        logger.warning(
            "Rejected Bitrix webhook event=%s token_sha256=%s token_length=%s",
            form.get("event"), hashlib.sha256(raw_token.encode()).hexdigest()[:12], len(raw_token),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid application token")

    event = str(form.get("event") or "")
    entity_id = form.get("data[FIELDS][ID]")
    if event.startswith("ONCRMLEAD"):
        entity_type = "lead"
    elif event.startswith("ONCRMDEAL"):
        entity_type = "deal"
    else:
        entity_type = None
    entry, already_processed = journal.begin(
        event,
        entity_type,
        str(entity_id) if entity_id else None,
        form.multi_items() if hasattr(form, "multi_items") else form.items(),
    )
    if already_processed:
        return {"status": "duplicate"}
    try:
        result = await _process_bitrix_event(event, str(entity_id) if entity_id else None)
        journal.mark_processed(entry["id"])
        return result
    except Exception as exc:
        journal.mark_failed(entry["id"], exc)
        raise


async def _bitrix_event_recovery_loop() -> None:
    """Retry durable events left pending/failed by network errors or restarts."""
    await asyncio.sleep(30)
    while True:
        for entry in journal.retryable():
            try:
                journal.mark_retrying(entry["id"])
                await _process_bitrix_event(entry["event_name"], entry["entity_id"])
                journal.mark_processed(entry["id"])
            except Exception as exc:
                journal.mark_failed(entry["id"], exc)
                logger.exception("Bitrix journal retry failed event_id=%s", entry["id"])
        await asyncio.sleep(30)


async def _notify_assigned_manager(client: BitrixClient, settings, lead_id: str, bitrix_user_id: str, assigned_name: str | None) -> None:
    """Если продажник привязан к Telegram — пишет ему в личку, что на него назначили лид."""
    manager_chat_id = store.manager_telegram_id(bitrix_user_id)
    if not manager_chat_id:
        return
    try:
        lead = await client.get_lead(lead_id)
        source_name = await client.get_source_name(lead.get("SOURCE_ID", ""))
        text = "📌 <b>На вас назначен лид</b>\n\n" + build_lead_notification(
            lead,
            portal_domain=_portal_domain(settings.bitrix_webhook_url),
            source_name=source_name,
            assigned_name=assigned_name,
        )
        await send_telegram_message(text, chat_id=manager_chat_id)
    except Exception:
        logger.exception("Failed to notify manager telegram_id=%s about lead_id=%s", manager_chat_id, lead_id)


async def _handle_message(message: dict, settings, update_id: int) -> None:
    if message.get("chat", {}).get("type") != "private":
        return
    user = message.get("from") or {}
    user_id = user.get("id")
    text = (message.get("text") or "").strip()

    if text == "/start":
        store.record_start(user_id, user.get("username"), user.get("first_name"))
        help_text = "Готово — вы будете получать уведомления от бота здесь."
        if user_id in settings.director_user_id_set:
            help_text += "\n\nДля ручной заявки отправьте номер телефона.\n/source — настроить источники\n/cancel — отменить ввод заявки"
        await send_telegram_message(help_text, chat_id=user_id)
        return

    if await manual.handle_main_message(message, update_id):
        return

    if user_id not in settings.admin_user_id_set:
        return

    if text == "/link":
        client = BitrixClient()
        users = await client.get_department_users(settings.sales_department_id)
        if not users:
            await send_telegram_message("В отделе продаж нет сотрудников.", chat_id=user_id)
            return
        await send_telegram_message(
            "Кого из продажников привязать к Telegram?",
            chat_id=user_id,
            reply_markup=build_link_pick_keyboard(users),
        )
        return

    if text == "/links":
        links = store.list_links()
        if not links:
            await send_telegram_message("Пока никто не привязан. Используйте /link.", chat_id=user_id)
            return
        lines = [f"{link['bitrix_name']} → <code>{link['telegram_id']}</code>" for link in links]
        await send_telegram_message("🔗 <b>Привязки</b>\n" + "\n".join(lines), chat_id=user_id)
        return


LINK_ACTIONS = {"link_pick", "link_to", "link_cancel"}
SOURCE_ACTIONS = {"mp", "mt"}


async def _handle_callback(callback_query: dict, settings, update_id: int) -> None:
    callback_id = callback_query["id"]
    from_user_id = callback_query["from"]["id"]

    data = callback_query.get("data", "")
    action_prefix = data.split(":", 1)[0]
    if action_prefix in LINK_ACTIONS:
        allowed_set = settings.admin_user_id_set
    elif action_prefix in SOURCE_ACTIONS:
        allowed_set = settings.admin_user_id_set | settings.director_user_id_set
    else:
        allowed_set = settings.director_user_id_set

    if from_user_id not in allowed_set:
        await answer_callback_query(callback_id, text="Нет доступа", show_alert=True)
        return

    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    original_text = message.get("text", "")

    logger.info(
        "Callback received: data=%s chat_id=%s message_id=%s from_user_id=%s",
        data, chat_id, message_id, from_user_id,
    )

    try:
        if action_prefix in manual.MAIN_ACTIONS:
            await answer_callback_query(callback_id)
            await manual.handle_main_callback(callback_query, update_id)
            return
        parts = data.split(":")
        action = parts[0]

        if action in {"dm", "db", "da", "dau", "dj"}:
            deal_id = parts[1]
            async with deals.lock:
                client = BitrixClient()
                if action == "dm":
                    await edit_message_reply_markup(chat_id, message_id, build_deal_action_keyboard(deal_id))
                elif action == "db":
                    await edit_message_reply_markup(chat_id, message_id, build_deal_manage_keyboard(deal_id))
                elif action == "da":
                    users = await client.get_department_users(settings.sales_department_id)
                    await edit_message_reply_markup(chat_id, message_id, build_deal_assign_keyboard(deal_id, users))
                elif action == "dau":
                    user_id = parts[2]
                    users = await client.get_department_users(settings.sales_department_id)
                    if user_id not in {str(user["ID"]) for user in users}:
                        await answer_callback_query(callback_id, text="Сотрудник больше не входит в отдел", show_alert=True)
                        return {"status": "ignored"}
                    await client.update_deal(deal_id, {"ASSIGNED_BY_ID": user_id})
                    deal = await client.get_deal(deal_id)
                    await deals.refresh_cards(deal)
                elif action == "dj":
                    deal = await client.move_deal_to_junk(deal_id)
                    await deals.refresh_cards(deal, is_junk=True)
            messages = {"dau": "Ответственный назначен", "dj": "Сделка отправлена на стадию «Мусор»"}
            await answer_callback_query(callback_id, text=messages.get(action))
            if action == "dau":
                try:
                    await deals.notify_assigned_manager(deal, user_id)
                except Exception:
                    logger.exception("Failed to notify assigned manager about deal_id=%s", deal_id)
            return {"status": "ok"}

        if action == "m":
            lead_id = parts[1]
            await edit_message_reply_markup(chat_id, message_id, build_action_keyboard(lead_id))
            await answer_callback_query(callback_id)

        elif action == "b":
            lead_id = parts[1]
            await edit_message_reply_markup(chat_id, message_id, build_manage_keyboard(lead_id))
            await answer_callback_query(callback_id)

        elif action == "a":
            lead_id = parts[1]
            client = BitrixClient()
            users = await client.get_department_users(settings.sales_department_id)
            await edit_message_reply_markup(chat_id, message_id, build_assign_keyboard(lead_id, users))
            await answer_callback_query(callback_id)

        elif action == "au":
            lead_id, user_id = parts[1], parts[2]
            client = BitrixClient()
            async with manual.lock:
                row = store.by_lead(lead_id)
                if row and row["state"] == "deleted":
                    await answer_callback_query(callback_id, text="Лид уже удалён", show_alert=True)
                    return
                users = await client.get_department_users(settings.sales_department_id)
                if user_id not in {str(user["ID"]) for user in users}:
                    await answer_callback_query(callback_id, text="Сотрудник больше не входит в отдел", show_alert=True)
                    return
                await client.update_lead(lead_id, {"ASSIGNED_BY_ID": user_id})
                assigned_name = await client.get_user_name(user_id)
                if row:
                    store.save(row["id"], manager=assigned_name or user_id, dirty=1)
                lead = await client.get_lead(lead_id)
                await stats.observe("lead", lead, client)
                source_name = await client.get_source_name(lead.get("SOURCE_ID", ""))
                new_text = build_lead_notification(
                    lead, portal_domain=_portal_domain(settings.bitrix_webhook_url),
                    source_name=source_name, assigned_name=assigned_name or user_id,
                )
                await edit_message_text(chat_id, message_id, new_text, reply_markup=build_manage_keyboard(lead_id))
                if row:
                    await manual.sync(store.submission(row["id"]))
            await answer_callback_query(callback_id, text="Ответственный назначен")
            await _notify_assigned_manager(client, settings, lead_id, user_id, assigned_name)

        elif action == "j":
            lead_id = parts[1]
            client = BitrixClient()
            async with manual.lock:
                lead = await client.move_to_junk(lead_id)
                await stats.observe("lead", lead, client)
                row = store.by_lead(lead_id)
                if row:
                    store.save(row["id"], state="junk", dirty=1)
                source_name = await client.get_source_name(lead.get("SOURCE_ID", ""))
                assigned_name = await client.get_user_name(str(lead.get("ASSIGNED_BY_ID") or ""))
                new_text = build_lead_notification(
                    lead, portal_domain=_portal_domain(settings.bitrix_webhook_url),
                    source_name=source_name, assigned_name=assigned_name, is_junk=True,
                )
                await edit_message_text(chat_id, message_id, new_text, reply_markup={"inline_keyboard": []})
                if row:
                    await manual.sync(store.submission(row["id"]))
            await answer_callback_query(callback_id, text="Лид перенесён в мусор")

        elif action == "link_pick":
            bitrix_user_id = parts[1]
            client = BitrixClient()
            bitrix_name = await client.get_user_name(bitrix_user_id) or bitrix_user_id
            starts = store.recent_starts()
            if not starts:
                await edit_message_text(chat_id, message_id, "Пока никто не писал боту /start.", reply_markup={"inline_keyboard": []})
                await answer_callback_query(callback_id)
                return
            await edit_message_text(
                chat_id, message_id,
                f"Привязать «{bitrix_name}» к какому Telegram-аккаунту?",
                reply_markup=build_link_target_keyboard(bitrix_user_id, bitrix_name, starts),
            )
            await answer_callback_query(callback_id)

        elif action == "link_to":
            bitrix_user_id, telegram_id = parts[1], int(parts[2])
            client = BitrixClient()
            bitrix_name = await client.get_user_name(bitrix_user_id) or bitrix_user_id
            store.link_manager(bitrix_user_id, bitrix_name, telegram_id)
            await edit_message_text(chat_id, message_id, f"✅ Привязано: {bitrix_name} → {telegram_id}", reply_markup={"inline_keyboard": []})
            await answer_callback_query(callback_id, text="Привязка сохранена")

        elif action == "link_cancel":
            await edit_message_text(chat_id, message_id, "Отменено.", reply_markup={"inline_keyboard": []})
            await answer_callback_query(callback_id)

        else:
            await answer_callback_query(callback_id)

    except BitrixApiError:
        logger.exception("Bitrix API call failed while handling callback data=%s", data)
        await answer_callback_query(callback_id, text="Ошибка Bitrix API", show_alert=True)
    except Exception:
        logger.exception("Failed to handle Telegram callback data=%s", data)
        await answer_callback_query(callback_id, text="Внутренняя ошибка", show_alert=True)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret")

    update = await request.json()

    message = update.get("message")
    if message is not None:
        await _handle_message(message, settings, update.get("update_id", 0))
        return {"status": "ok"}

    callback_query = update.get("callback_query")
    if callback_query is not None:
        await _handle_callback(callback_query, settings, update.get("update_id", 0))
        return {"status": "ok"}

    return {"status": "ignored"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
