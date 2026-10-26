"""Persistent Telegram flow for legacy missed-call distribution.

The flow is intentionally enabled only for MODE_SWITCH_USER_ID during the
parallel migration. Sessions live in the Lead Flow database, not in memory.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from app import store
from app import planner_access
from app.bitrix_client import BitrixClient
from app.distribution import PERIODS, Distribution, ManagerAllocation, best_distributions, parse_manual_date, previous_days
from app.manager_planner import api as manager_api
from app.manager_planner.distribution_import import import_snapshot
from app.manager_planner.distribution_queue import BINDINGS
from app.sales_team import assignable_sales_users, user_name
from app.telegram_client import answer_callback_query, edit_message_text, send_telegram_message

MOSCOW = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)
STAGES = (
    ("Недозвоны", "IN_PROCESS"),
    ("Неудобно говорить", "PROCESSED"),
    ("Не обработан", "NEW"),
)


def _callback(session_id: str, action: str, value: int | None = None) -> str:
    suffix = f":{value}" if value is not None else ""
    return f"dist:{session_id}:{action}{suffix}"


def _keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def _manager_keyboard(session_id: str, managers: list[dict], selected: list[int]) -> dict:
    rows = [[{
        "text": f"{'☑️' if index in selected else '⬜'} {manager['name']}",
        "callback_data": _callback(session_id, "manager", index),
    }] for index, manager in enumerate(managers)]
    rows.append([{"text": "Продолжить", "callback_data": _callback(session_id, "managers_done")}])
    return _keyboard(rows)


def _stage_keyboard(session_id: str, selected: list[int]) -> dict:
    rows = [[{
        "text": f"{'☑️' if index in selected else '⬜'} {label}",
        "callback_data": _callback(session_id, "stage", index),
    }] for index, (label, _) in enumerate(STAGES)]
    rows.extend([
        [{"text": "Продолжить", "callback_data": _callback(session_id, "stages_done")}],
        [{"text": "🔄 Новое распределение", "callback_data": _callback(session_id, "new")}],
    ])
    return _keyboard(rows)


def _period_keyboard(session_id: str) -> dict:
    rows = [[{
        "text": f"{number} {'дня' if number < 5 else 'дней'}",
        "callback_data": _callback(session_id, "period", number),
    }] for number in PERIODS]
    rows.append([{"text": "🔄 Новое распределение", "callback_data": _callback(session_id, "new")}])
    return _keyboard(rows)


def _empty_keyboard(session_id: str) -> dict:
    return _keyboard([
        [{"text": "➕ Добавить предыдущий день", "callback_data": _callback(session_id, "previous")}],
        [{"text": "📅 Выбрать другую дату", "callback_data": _callback(session_id, "manual")}],
        [{"text": "Продолжить без замены", "callback_data": _callback(session_id, "skip")}],
        [{"text": "🔄 Новое распределение", "callback_data": _callback(session_id, "new")}],
    ])


def _can_confirm(payload: dict) -> bool:
    selected = [payload["managers"][index] for index in payload.get("selected", [])]
    return (
        payload.get("stage_ids") == ["IN_PROCESS"]
        and 0 < len(selected) <= 3
        and all(int(manager["bitrix_id"]) in set(BINDINGS.values()) for manager in selected)
    )


def _may_confirm(user_id: int, settings) -> bool:
    return user_id in getattr(
        settings,
        "distribution_confirmer_id_set",
        {getattr(settings, "mode_switch_user_id", 1297686797)},
    )


def _result_keyboard(session_id: str, has_next: bool, can_confirm: bool = False) -> dict:
    rows = []
    if can_confirm:
        rows.append([{"text": "✅ Подтвердить в Planner", "callback_data": _callback(session_id, "confirm")}])
    if has_next:
        rows.append([{"text": "🔀 Другой результат", "callback_data": _callback(session_id, "next")}])
    rows.append([{"text": "🔄 Новое распределение", "callback_data": _callback(session_id, "new")}])
    return _keyboard(rows)


async def _sales_managers(settings) -> list[dict]:
    users = assignable_sales_users(
        await BitrixClient().get_department_users(settings.sales_department_id), settings,
    )
    managers = [
        {"bitrix_id": str(user["ID"]), "name": user_name(user)}
        for user in users if user.get("ID") and user_name(user)
    ]
    managers.sort(key=lambda item: item["name"].casefold())
    return managers


async def start(user_id: int, chat_id: int, settings, *, edit_message_id: int | None = None) -> None:
    try:
        managers = await _sales_managers(settings)
    except Exception:
        await send_telegram_message("Не удалось получить менеджеров отдела продаж из Bitrix.", chat_id=chat_id)
        return
    if not managers:
        await send_telegram_message("В отделе продаж нет доступных менеджеров.", chat_id=chat_id)
        return
    session_id = secrets.token_hex(6)
    payload = {"managers": managers, "selected": [], "selected_stages": [0]}
    store.save_distribution_session(user_id, session_id, "managers", payload)
    text = "Выберите от одного до трёх менеджеров:"
    keyboard = _manager_keyboard(session_id, managers, [])
    if edit_message_id is None:
        await send_telegram_message(text, chat_id=chat_id, reply_markup=keyboard)
    else:
        await edit_message_text(chat_id, edit_message_id, text, reply_markup=keyboard)


def _session(user_id: int, session_id: str) -> dict | None:
    session = store.distribution_session(user_id)
    return session if session and session["session_id"] == session_id else None


def _save(user_id: int, session: dict, step: str | None = None) -> None:
    store.save_distribution_session(
        user_id, session["session_id"], step or session["step"], session["payload"],
    )
    session["step"] = step or session["step"]


def _dates(payload: dict) -> dict[date, int]:
    return {date.fromisoformat(key): int(value) for key, value in payload.get("counts", {}).items()}


def _stage_counts(payload: dict) -> dict[date, dict[str, int]]:
    return {date.fromisoformat(key): value for key, value in payload.get("stage_counts", {}).items()}


def _serialize_variant(value: Distribution) -> dict:
    return {
        "spread": value.spread,
        "allocations": [{
            "manager": allocation.manager,
            "days": [[day.isoformat(), count] for day, count in allocation.days],
        } for allocation in value.allocations],
    }


def _deserialize_variant(value: dict) -> Distribution:
    allocations = tuple(ManagerAllocation(
        item["manager"], tuple((date.fromisoformat(day), int(count)) for day, count in item["days"]),
    ) for item in value["allocations"])
    return Distribution(allocations, int(value["spread"]), 0, 0)


def _format_result(result: Distribution, index: int, total: int, payload: dict) -> str:
    stage_ids = payload["stage_ids"]
    stage_names = payload["stage_names"]
    breakdown = _stage_counts(payload)
    dates = sorted(day for allocation in result.allocations for day, _ in allocation.days)
    period = f"{dates[0]:%d.%m.%Y}–{dates[-1]:%d.%m.%Y}" if len(dates) > 1 else f"{dates[0]:%d.%m.%Y}"
    lines = [
        "📊 Распределение недозвонов" if stage_names == ["Недозвоны"] else "📊 Распределение лидов",
        "Стадии: " + ", ".join(stage_names), "", "Период:", period,
        "Учитываются только даты, перечисленные ниже.", "", f"Всего лидов: {result.total}",
    ]
    for allocation in result.allocations:
        lines += ["", f"👤 {allocation.manager}"]
        for day, count in allocation.days:
            if len(stage_ids) > 1:
                details = ", ".join(
                    f"{name.lower()} {breakdown[day][stage_id]}"
                    for name, stage_id in zip(stage_names, stage_ids, strict=True)
                )
                lines.append(f"{day:%d.%m.%Y} — {details}")
            else:
                lines.append(f"{day:%d.%m.%Y} — {count}")
        if not allocation.days:
            lines.append("Нет назначенных дат.")
        lines.append(f"Итого: {allocation.total}")
    lines += ["", f"Разница нагрузки: {result.spread}", f"Вариант {index + 1} из {total}"]
    return "\n".join(lines)


async def _finish(user_id: int, chat_id: int, message_id: int, session: dict, settings, notice: str = "") -> None:
    payload = session["payload"]
    names = [payload["managers"][index]["name"] for index in payload["selected"]]
    variants = await asyncio.to_thread(best_distributions, _dates(payload), names)
    payload["variants"] = [_serialize_variant(item) for item in variants]
    payload["variant_index"] = 0
    _save(user_id, session, "result")
    text = _format_result(variants[0], 0, len(variants), payload) if variants else "Нет непустых дат для распределения."
    if notice:
        text = notice + "\n\n" + text
    await edit_message_text(
        chat_id, message_id, text,
        reply_markup=_result_keyboard(
            session["session_id"], len(variants) > 1,
            _can_confirm(payload) and _may_confirm(user_id, settings),
        ),
    )


async def _show_empty(user_id: int, chat_id: int, message_id: int, session: dict, notice: str = "") -> None:
    payload = session["payload"]
    text = _empty_text(payload, notice)
    _save(user_id, session, "empty_days")
    await edit_message_text(chat_id, message_id, text, reply_markup=_empty_keyboard(session["session_id"]))


def _empty_text(payload: dict, notice: str = "") -> str:
    counts = _dates(payload)
    empty = [date.fromisoformat(value) for value in payload["empty_dates"]]
    lines = [notice] if notice else []
    lines += [
        "Пустые даты исходного периода исключены:",
        ", ".join(f"{day:%d.%m.%Y}" for day in empty), "",
        "Стадии: " + ", ".join(payload["stage_names"]), "Даты с лидами:",
    ]
    lines += [f"{day:%d.%m.%Y} — {count}" for day, count in sorted(counts.items())] or ["Пока нет."]
    lines += ["", f"Не хватает непустых дат: {payload['target_count'] - len(counts)}.", "Добавьте замену или продолжите без неё."]
    return "\n".join(lines)


async def handle_callback(callback_query: dict, settings) -> bool:
    data = callback_query.get("data", "")
    if not data.startswith("dist:"):
        return False
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    callback_id = callback_query["id"]
    parts = data.split(":")
    if len(parts) < 3:
        await answer_callback_query(callback_id, text="Некорректная кнопка", show_alert=True)
        return True
    session_id, action = parts[1], parts[2]
    if action == "new":
        await answer_callback_query(callback_id)
        await start(user_id, chat_id, settings, edit_message_id=message_id)
        return True
    session = _session(user_id, session_id)
    if not session:
        await answer_callback_query(callback_id, text="Кнопка устарела. Запустите /distribution заново.", show_alert=True)
        return True
    payload = session["payload"]
    if action == "manager":
        if len(parts) != 4 or not parts[3].isdigit() or int(parts[3]) not in range(len(payload["managers"])):
            await answer_callback_query(callback_id, text="Неизвестный менеджер", show_alert=True)
            return True
        index = int(parts[3])
        selected = set(payload["selected"])
        if index in selected:
            selected.remove(index)
        elif len(selected) >= 3:
            await answer_callback_query(callback_id, text="Можно выбрать не более трёх менеджеров.", show_alert=True)
            return True
        else:
            selected.add(index)
        payload["selected"] = sorted(selected)
        _save(user_id, session)
        await edit_message_text(chat_id, message_id, "Выберите от одного до трёх менеджеров:", reply_markup=_manager_keyboard(session_id, payload["managers"], payload["selected"]))
        await answer_callback_query(callback_id)
    elif action == "managers_done":
        if not payload["selected"]:
            await answer_callback_query(callback_id, text="Выберите хотя бы одного менеджера.", show_alert=True)
            return True
        _save(user_id, session, "stages")
        await edit_message_text(chat_id, message_id, "Выберите одну или несколько стадий:", reply_markup=_stage_keyboard(session_id, payload["selected_stages"]))
        await answer_callback_query(callback_id)
    elif action == "stage":
        if len(parts) != 4 or not parts[3].isdigit() or int(parts[3]) not in range(len(STAGES)):
            await answer_callback_query(callback_id, text="Неизвестная стадия", show_alert=True)
            return True
        index = int(parts[3])
        selected = set(payload["selected_stages"])
        selected.symmetric_difference_update({index})
        payload["selected_stages"] = sorted(selected)
        _save(user_id, session)
        await edit_message_text(chat_id, message_id, "Выберите одну или несколько стадий:", reply_markup=_stage_keyboard(session_id, payload["selected_stages"]))
        await answer_callback_query(callback_id)
    elif action == "stages_done":
        if not payload["selected_stages"]:
            await answer_callback_query(callback_id, text="Выберите хотя бы одну стадию.", show_alert=True)
            return True
        payload["stage_names"] = [STAGES[index][0] for index in payload["selected_stages"]]
        payload["stage_ids"] = [STAGES[index][1] for index in payload["selected_stages"]]
        _save(user_id, session, "period")
        await edit_message_text(chat_id, message_id, "Выберите период. Отсчёт начинается со вчерашнего дня.", reply_markup=_period_keyboard(session_id))
        await answer_callback_query(callback_id)
    elif action == "period":
        if len(parts) != 4 or not parts[3].isdigit():
            await answer_callback_query(callback_id, text="Недопустимый период", show_alert=True)
            return True
        days = int(parts[3])
        if days not in PERIODS:
            await answer_callback_query(callback_id, text="Недопустимый период", show_alert=True)
            return True
        await answer_callback_query(callback_id, text="Получаю данные из Bitrix…")
        dates = previous_days(days, MOSCOW)
        client = BitrixClient()
        breakdown = await client.distribution_counts_by_stage(dates, payload["stage_ids"], timezone=MOSCOW)
        counts = {day: sum(values.values()) for day, values in breakdown.items()}
        payload.update({
            "target_count": days,
            "counts": {day.isoformat(): count for day, count in counts.items() if count},
            "stage_counts": {day.isoformat(): values for day, values in breakdown.items() if counts[day]},
            "empty_dates": [day.isoformat() for day, count in counts.items() if not count],
            "search_before": min(dates).isoformat(),
        })
        if len(payload["counts"]) == days:
            await _finish(user_id, chat_id, message_id, session, settings)
        else:
            await _show_empty(user_id, chat_id, message_id, session)
    elif action == "previous":
        await answer_callback_query(callback_id, text="Ищу предыдущий день…")
        before = date.fromisoformat(payload["search_before"])
        found = await BitrixClient().find_previous_distribution_day(
            before, payload["stage_ids"], excluded=_dates(payload), timezone=MOSCOW,
        )
        if not found:
            await _show_empty(user_id, chat_id, message_id, session, "Более ранних дат с лидами не найдено.")
            return True
        day, breakdown = found
        count = sum(breakdown.values())
        payload["counts"][day.isoformat()] = count
        payload["stage_counts"][day.isoformat()] = breakdown
        payload["search_before"] = day.isoformat()
        notice = f"Добавлена дата {day:%d.%m.%Y} — {count}."
        if len(payload["counts"]) >= payload["target_count"]:
            await _finish(user_id, chat_id, message_id, session, settings, notice)
        else:
            await _show_empty(user_id, chat_id, message_id, session, notice)
    elif action == "manual":
        _save(user_id, session, "manual_date")
        await edit_message_text(
            chat_id, message_id,
            "Введите дату следующим сообщением: ДД.ММ.ГГГГ или ДД.ММ.\nСегодняшние и будущие даты не учитываются.",
            reply_markup=_empty_keyboard(session_id),
        )
        await answer_callback_query(callback_id)
    elif action == "skip":
        await answer_callback_query(callback_id)
        await _finish(user_id, chat_id, message_id, session, settings)
    elif action == "next":
        variants = payload.get("variants", [])
        index = int(payload.get("variant_index", 0)) + 1
        if index >= len(variants):
            await answer_callback_query(callback_id, text="Других вариантов нет.", show_alert=True)
            return True
        payload["variant_index"] = index
        _save(user_id, session)
        await edit_message_text(
            chat_id, message_id, _format_result(_deserialize_variant(variants[index]), index, len(variants), payload),
            reply_markup=_result_keyboard(
                session_id, index + 1 < len(variants),
                _can_confirm(payload) and _may_confirm(user_id, settings),
            ),
        )
        await answer_callback_query(callback_id)
    elif action == "confirm":
        if not _may_confirm(user_id, settings):
            await answer_callback_query(callback_id, text="У вас нет права подтверждать распределение.", show_alert=True)
            return True
        if not _can_confirm(payload):
            await answer_callback_query(callback_id, text="Для импорта выберите привязанных менеджеров и только стадию «Недозвоны».", show_alert=True)
            return True
        await answer_callback_query(callback_id, text="Проверяю актуальные лиды…")
        variant_index = int(payload.get("variant_index", 0))
        variant = _deserialize_variant(payload["variants"][variant_index])
        reverse_bindings = {bitrix_id: telegram_id for telegram_id, bitrix_id in BINDINGS.items()}
        allocations = []
        seen_leads: set[int] = set()
        for allocation in variant.allocations:
            manager = next(item for item in payload["managers"] if item["name"] == allocation.manager)
            bitrix_id = int(manager["bitrix_id"])
            leads = []
            assigned_dates = []
            for day, expected in allocation.days:
                current = await BitrixClient().get_distribution_leads(day, day, ["IN_PROCESS"], timezone=MOSCOW)
                if len(current) != expected:
                    await send_telegram_message(
                        "Количество лидов изменилось. Запустите новое распределение.", chat_id=chat_id,
                    )
                    return True
                assigned_dates.append(day.isoformat())
                for lead in current:
                    if lead.id in seen_leads:
                        raise ValueError("Duplicate lead in distribution")
                    seen_leads.add(lead.id)
                    leads.append({"id": lead.id, "created_date": day.isoformat(), "stage_id": lead.stage_id})
            allocations.append({
                "telegram_id": reverse_bindings[bitrix_id], "bitrix_id": bitrix_id,
                "name": allocation.manager, "dates": assigned_dates,
                "leads": sorted(leads, key=lambda item: item["id"]), "count": len(leads),
            })
        identifier = str(uuid5(NAMESPACE_URL, f"lead-flow:{user_id}:{session_id}:{variant_index}"))
        now = datetime.now(MOSCOW)
        snapshot = {
            "schema": 1, "distribution_id": identifier, "workday": now.date().isoformat(),
            "confirmed_at": now.isoformat(), "confirmed_by": user_id,
            "variant_index": variant_index, "variant_id": identifier,
            "stage_id": "IN_PROCESS", "allocations": allocations, "total": len(seen_leads),
        }
        result = import_snapshot(manager_api.store, snapshot, {user_id})
        await send_telegram_message(
            "✅ Распределение импортировано в Planner. Задачи добавлены менеджерам."
            if result["status"] == "imported"
            else "⚠️ Распределение сохранено для проверки, задачи не созданы.",
            chat_id=chat_id,
        )
    else:
        await answer_callback_query(callback_id, text="Кнопка устарела", show_alert=True)
    return True


async def handle_message(message: dict, settings) -> bool:
    user_id = (message.get("from") or {}).get("id")
    session = store.distribution_session(user_id) if user_id else None
    if not session or session["step"] != "manual_date" or not message.get("text"):
        return False
    payload = session["payload"]
    try:
        day = parse_manual_date(message["text"], datetime.now(MOSCOW).date())
    except ValueError as exc:
        await send_telegram_message(str(exc), chat_id=message["chat"]["id"])
        return True
    if day.isoformat() in payload["counts"]:
        await send_telegram_message("Эта дата уже добавлена. Выберите другую.", chat_id=message["chat"]["id"])
        return True
    breakdown = (await BitrixClient().distribution_counts_by_stage([day], payload["stage_ids"], timezone=MOSCOW))[day]
    count = sum(breakdown.values())
    if not count:
        await send_telegram_message(f"{day:%d.%m.%Y} — 0 лидов выбранных стадий. Введите другую дату.", chat_id=message["chat"]["id"])
        return True
    payload["counts"][day.isoformat()] = count
    payload["stage_counts"][day.isoformat()] = breakdown
    # A new message is used because Telegram only allows editing the bot's own message.
    if len(payload["counts"]) < payload["target_count"]:
        _save(user_id, session, "empty_days")
        await send_telegram_message(
            _empty_text(payload, f"Добавлена дата {day:%d.%m.%Y} — {count}."),
            chat_id=message["chat"]["id"], reply_markup=_empty_keyboard(session["session_id"]),
        )
        return True
    variants = await asyncio.to_thread(
        best_distributions, _dates(payload), [payload["managers"][i]["name"] for i in payload["selected"]],
    )
    payload["variants"] = [_serialize_variant(item) for item in variants]
    payload["variant_index"] = 0
    _save(user_id, session, "result")
    await send_telegram_message(
        f"Добавлена дата {day:%d.%m.%Y} — {count}.\n\n" + _format_result(variants[0], 0, len(variants), payload),
        chat_id=message["chat"]["id"],
        reply_markup=_result_keyboard(
            session["session_id"], len(variants) > 1,
            _can_confirm(payload) and _may_confirm(user_id, settings),
        ),
    )
    return True


async def send_due_reminders(settings, *, now: datetime | None = None) -> int:
    current = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    if current.hour != 10 or current.minute >= 10:
        return 0
    sent = 0
    allowed_users = planner_access.enabled_user_ids() | {
        getattr(settings, "mode_switch_user_id", 1297686797)
    }
    for reminder in store.distribution_reminders():
        if reminder["enabled_by"] not in allowed_users:
            continue
        if not store.claim_distribution_reminder(
            reminder["chat_id"], reminder["thread_id"], current.date().isoformat(),
        ):
            continue
        try:
            await send_telegram_message(
                "Доброе утро! Пора распределить недозвоны.",
                chat_id=reminder["chat_id"],
                reply_markup=_keyboard([[{
                    "text": "📊 Распределить недозвоны",
                    "callback_data": "dist:reminder:new",
                }]]),
            )
            sent += 1
        except Exception:
            logger.exception("Failed to send distribution reminder chat_id=%s", reminder["chat_id"])
    return sent


async def reminder_loop(settings) -> None:
    while True:
        try:
            await send_due_reminders(settings)
        except Exception:
            logger.exception("Distribution reminder loop failed")
        await asyncio.sleep(30)
