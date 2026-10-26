"""FastAPI service for Bitrix24 leads/deals, Telegram notifications and management."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status

from app import call_tracking, deals, distribution_flow, journal, manual, miniapp, outcomes, period_reports, planner_access, reconcile, reports, stats, store
from app.manager_planner.api import app as manager_planner_app, initialize as initialize_manager_planner
from app.manager_planner import api as manager_planner_api, sync as planner_distribution_sync
from app.bitrix_client import (
    LEAD_PREFERRED_CALL_TIME_FIELD,
    BitrixApiError,
    BitrixClient,
    close_http_client,
)
from app.config import get_settings, lead_notification_recipient_ids
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
from app.sales_team import assignable_sales_users
from app.telegram_client import (
    answer_callback_query,
    edit_message_reply_markup,
    edit_message_text,
    send_telegram_message,
    set_chat_menu_button,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot-lead-flow")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    planner_access.seed_legacy_identities()
    initialize_manager_planner()
    menu_user_ids = settings.mini_app_user_id_set | planner_access.enabled_user_ids()
    for account_id in menu_user_ids:
        try:
            url, label = _mini_app_destination(settings, account_id)
            await set_chat_menu_button(url, chat_id=account_id, text=label)
        except Exception:
            logger.exception("Failed to configure Mini App menu button for user_id=%s", account_id)
    tasks = [
        asyncio.create_task(deals.polling_loop()),
        asyncio.create_task(manual.recovery_loop()),
        asyncio.create_task(reports.daily_report_loop()),
        asyncio.create_task(reconcile.reconciliation_loop()),
        asyncio.create_task(outcomes.daily_sync_loop()),
        asyncio.create_task(_bitrix_event_recovery_loop()),
        asyncio.create_task(distribution_flow.reminder_loop(settings)),
    ]
    legacy_queue = str(getattr(settings, "legacy_distribution_queue_path", "") or "").strip()
    if legacy_queue:
        confirmer_ids = getattr(settings, "legacy_distribution_confirmer_id_set", set())
        if not confirmer_ids:
            raise RuntimeError("LEGACY_DISTRIBUTION_CONFIRMER_IDS must be explicit when the legacy queue bridge is enabled")
        tasks.append(asyncio.create_task(planner_distribution_sync.sync_loop(
            manager_planner_api.store,
            Path(legacy_queue),
            confirmer_ids,
            float(getattr(settings, "legacy_distribution_poll_seconds", 30)),
        )))
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task
    await close_http_client()


app = FastAPI(title="bot-lead-flow", lifespan=lifespan)
app.include_router(miniapp.router)
app.include_router(call_tracking.router)
app.mount("/manager", manager_planner_app)


def _mini_app_destination(settings, user_id: int) -> tuple[str, str]:
    switch_user_id = getattr(settings, "mode_switch_user_id", 1297686797)
    identity = planner_access.authorized_identity(user_id)
    manager_mode = (
        user_id == switch_user_id and store.user_ui_mode(user_id) == "manager"
    ) or (identity is not None and identity["role"] == "manager")
    if manager_mode:
        return getattr(settings, "manager_app_url", "https://tg.prav-buro.ru/manager/"), "План дня"
    if identity is not None and identity["role"] == "supervisor":
        return getattr(settings, "manager_app_url", "https://tg.prav-buro.ru/manager/"), "Планы"
    return settings.mini_app_url, "Отчёты"


def _mini_app_keyboard(settings, user_id: int) -> dict:
    url, label = _mini_app_destination(settings, user_id)
    identity = planner_access.authorized_identity(user_id)
    open_button = {
        "text": "🗓 Открыть план дня" if label == "План дня" else "📊 Открыть отчёты",
        "web_app": {"url": url},
    }
    if user_id == getattr(settings, "mode_switch_user_id", 1297686797):
        current = store.user_ui_mode(user_id)
        return {
            "inline_keyboard": [
                [open_button],
                _mode_keyboard(current)["inline_keyboard"][0],
            ],
        }
    if identity is not None and identity["role"] == "supervisor":
        return {
            "inline_keyboard": [
                [{
                    "text": "🗓 Планы сотрудников",
                    "web_app": {"url": url},
                }],
                [{"text": "📊 Отчёты Lead Flow", "web_app": {"url": settings.mini_app_url}}],
            ],
        }
    return {
        "inline_keyboard": [[open_button]],
    }


def _mode_keyboard(current: str) -> dict:
    return {"inline_keyboard": [[
        {"text": ("✓ " if current == "supervisor" else "") + "Руководитель", "callback_data": "mode:supervisor"},
        {"text": ("✓ " if current == "manager" else "") + "Менеджер", "callback_data": "mode:manager"},
    ]]}


def _distribution_user_ids(settings) -> set[int]:
    return planner_access.enabled_user_ids() | {
        getattr(settings, "mode_switch_user_id", 1297686797)
    }


def _portal_domain(webhook_url: str) -> str | None:
    """Достаёт домен портала из адреса входящего вебхука, чтобы собрать ссылку на лид."""
    host = urlparse(webhook_url).netloc
    return host or None


def _comment_with_preferred_call_time(lead: dict) -> str | None:
    """Добавляет выбранное клиентом время звонка, не дублируя строку при повторе события."""
    raw_value = lead.get(LEAD_PREFERRED_CALL_TIME_FIELD)
    if isinstance(raw_value, list):
        call_time = ", ".join(str(value).strip() for value in raw_value if str(value).strip())
    else:
        call_time = str(raw_value or "").strip()
    if not call_time:
        return None

    line = f"Желаемое время звонка: {call_time}"
    comments = str(lead.get("COMMENTS") or "").strip()
    if line.casefold() in comments.casefold():
        return None
    return f"{comments}\n{line}" if comments else line


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
        if event == "ONCRMLEADADD":
            enriched_comments = _comment_with_preferred_call_time(lead)
            if enriched_comments is not None:
                await client.update_lead(lead_id, {"COMMENTS": enriched_comments})
                lead = {**lead, "COMMENTS": enriched_comments}
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
    for admin_id in lead_notification_recipient_ids(settings):
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
    # Исключение из общего потока новых лидов не должно скрывать личное
    # уведомление: назначенный менеджер обязан увидеть свою заявку.
    manager_chat_ids = store.manager_telegram_ids(bitrix_user_id)
    if not manager_chat_ids:
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
    except Exception:
        logger.exception("Failed to prepare manager notification about lead_id=%s", lead_id)
        return
    for manager_chat_id in manager_chat_ids:
        try:
            await send_telegram_message(text, chat_id=manager_chat_id)
        except Exception:
            logger.exception("Failed to notify manager telegram_id=%s about lead_id=%s", manager_chat_id, lead_id)


async def _handle_message(message: dict, settings, update_id: int) -> None:
    if message.get("chat", {}).get("type") != "private":
        return
    user = message.get("from") or {}
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    mini_app_users = getattr(
        settings,
        "mini_app_user_id_set",
        settings.director_user_id_set
        | settings.admin_user_id_set
        | getattr(settings, "super_admin_user_id_set", set()),
    ) | planner_access.enabled_user_ids()

    if text == "/start":
        store.record_start(user_id, user.get("username"), user.get("first_name"))
        help_text = "Готово — вы будете получать уведомления от бота здесь."
        if user_id in mini_app_users:
            help_text += "\n\n/app — интерактивные отчёты"
        if user_id == getattr(settings, "mode_switch_user_id", 1297686797):
            help_text += (
                "\n/mode — переключить режим руководителя/менеджера"
                "\n/planner_users — перенесённые роли и состояние входа"
                "\n/planner_enable ID и /planner_disable ID — открыть/закрыть Planner"
            )
        if user_id in _distribution_user_ids(settings):
            help_text += (
                "\n/distribution — распределить недозвоны"
                "\n/reminders_on и /reminders_off — напоминание в 10:00 МСК"
            )
        if user_id in settings.director_user_id_set:
            help_text += (
                "\nДля ручной заявки отправьте номер телефона."
                "\n/period ДД.ММ.ГГГГ - ДД.ММ.ГГГГ — текущий результат обращений"
                "\n/source — настроить источники\n/cancel — отменить ввод заявки"
            )
        if user_id in settings.admin_user_id_set:
            help_text += "\n/link — привязать менеджера\n/links — посмотреть привязки"
        await send_telegram_message(
            help_text,
            chat_id=user_id,
            reply_markup=_mini_app_keyboard(settings, user_id) if user_id in mini_app_users else None,
        )
        return

    command = text.split()[0].split("@")[0].lower() if text else ""
    if command == "/mode" and user_id == getattr(settings, "mode_switch_user_id", 1297686797):
        current = store.user_ui_mode(user_id)
        await send_telegram_message(
            "Выберите, какой Mini App открывать: руководительские отчёты или личный план менеджера.",
            chat_id=user_id,
            reply_markup=_mode_keyboard(current),
        )
        return

    if command == "/distribution" and user_id in _distribution_user_ids(settings):
        await distribution_flow.start(user_id, message["chat"]["id"], settings)
        return

    if command in {"/reminders_on", "/reminders_off"} and user_id in _distribution_user_ids(settings):
        enabled = command == "/reminders_on"
        store.set_distribution_reminder(message["chat"]["id"], user_id, enabled)
        await send_telegram_message(
            "🔔 Напоминание включено: ежедневно в 10:00 по Москве."
            if enabled else "🔕 Напоминание о распределении выключено.",
            chat_id=message["chat"]["id"],
        )
        return

    if command == "/planner_users" and user_id == getattr(settings, "mode_switch_user_id", 1297686797):
        role_labels = {"owner": "владелец", "manager": "менеджер", "supervisor": "руководитель"}
        lines = ["👥 <b>Доступ к Planner</b>"]
        for identity in store.list_planner_access():
            state = "вход открыт" if identity["login_enabled"] and identity["active"] else "вход закрыт"
            if not identity["active"]:
                state = "отключён"
            bitrix = f" · Bitrix {identity['bitrix_user_id']}" if identity["bitrix_user_id"] else ""
            lines.append(
                f"• {identity['display_name']} — {role_labels[identity['role']]} · "
                f"<code>{identity['telegram_id']}</code>{bitrix} · {state}"
            )
        await send_telegram_message("\n".join(lines), chat_id=user_id)
        return

    if command in {"/planner_enable", "/planner_disable"} and user_id == getattr(settings, "mode_switch_user_id", 1297686797):
        parts = text.split()
        try:
            target_id = int(parts[1]) if len(parts) == 2 else 0
        except ValueError:
            target_id = 0
        identity = store.planner_access(target_id) if target_id else None
        if identity is None:
            await send_telegram_message(
                "Укажите Telegram ID из /planner_users.", chat_id=user_id,
            )
            return
        if command == "/planner_enable" and not identity["active"]:
            await send_telegram_message(
                f"{identity['display_name']}: учётная запись неактивна, вход не открыт.",
                chat_id=user_id,
            )
            return
        enabled = command == "/planner_enable"
        store.set_planner_login_enabled(target_id, enabled)
        if enabled:
            url, label = _mini_app_destination(settings, target_id)
            await set_chat_menu_button(url, chat_id=target_id, text=label)
        await send_telegram_message(
            f"{identity['display_name']}: вход в Planner {'открыт' if enabled else 'закрыт'}.",
            chat_id=user_id,
        )
        return

    if command == "/app" and user_id in mini_app_users:
        _url, label = _mini_app_destination(settings, user_id)
        await send_telegram_message(
            (
                "🗓 Откройте личный план дня."
                if label == "План дня"
                else "🗓 Откройте планы сотрудников или отчёты Lead Flow."
                if label == "Планы"
                else "📊 Выберите период и посмотрите актуальные стадии обращений."
            ),
            chat_id=user_id,
            reply_markup=_mini_app_keyboard(settings, user_id),
        )
        return

    if command == "/period" and user_id in settings.director_user_id_set:
        period = outcomes.parse_period(text)
        if not period:
            await send_telegram_message(
                "Формат: <code>/period 01.09.2026 - 23.09.2026</code>", chat_id=user_id,
            )
        else:
            started = period_reports.enqueue_period_report(*period, chat_id=user_id)
            await send_telegram_message(
                (
                    "⏳ Сверяю лиды, сделки и договоры с Bitrix. Excel пришлю сюда после завершения."
                    if started else "⏳ Ваш предыдущий отчёт ещё формируется."
                ),
                chat_id=user_id,
            )
        return

    if user_id in _distribution_user_ids(settings) and await distribution_flow.handle_message(message, settings):
        return

    if await manual.handle_main_message(message, update_id):
        return

    if user_id not in settings.admin_user_id_set:
        return

    if text == "/link":
        client = BitrixClient()
        users = assignable_sales_users(
            await client.get_department_users(settings.sales_department_id), settings,
        )
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
MODE_ACTIONS = {"mode"}
DISTRIBUTION_ACTIONS = {"dist"}


async def _handle_callback(callback_query: dict, settings, update_id: int) -> None:
    callback_id = callback_query["id"]
    from_user_id = callback_query["from"]["id"]

    data = callback_query.get("data", "")
    action_prefix = data.split(":", 1)[0]
    if action_prefix in LINK_ACTIONS:
        allowed_set = settings.admin_user_id_set
    elif action_prefix in MODE_ACTIONS:
        allowed_set = {getattr(settings, "mode_switch_user_id", 1297686797)}
    elif action_prefix in DISTRIBUTION_ACTIONS:
        allowed_set = _distribution_user_ids(settings)
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
        if action_prefix == "dist":
            await distribution_flow.handle_callback(callback_query, settings)
            return
        if action_prefix == "mode":
            mode = data.split(":", 1)[1] if ":" in data else ""
            if mode not in {"supervisor", "manager"}:
                await answer_callback_query(callback_id, text="Неизвестный режим", show_alert=True)
                return
            store.set_user_ui_mode(from_user_id, mode)
            url, label = _mini_app_destination(settings, from_user_id)
            await set_chat_menu_button(url, chat_id=from_user_id, text=label)
            await edit_message_text(
                chat_id,
                message_id,
                "Режим переключён: менеджер." if mode == "manager" else "Режим переключён: руководитель.",
                reply_markup=_mini_app_keyboard(settings, from_user_id),
            )
            await answer_callback_query(callback_id, text="Режим сохранён")
            return
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
                    users = assignable_sales_users(
                        await client.get_department_users(settings.sales_department_id), settings,
                    )
                    await edit_message_reply_markup(chat_id, message_id, build_deal_assign_keyboard(deal_id, users))
                elif action == "dau":
                    user_id = parts[2]
                    users = assignable_sales_users(
                        await client.get_department_users(settings.sales_department_id), settings,
                    )
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
            users = assignable_sales_users(
                await client.get_department_users(settings.sales_department_id), settings,
            )
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
                users = assignable_sales_users(
                    await client.get_department_users(settings.sales_department_id), settings,
                )
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
