"""Разбор полей лида Bitrix24 и сборка красивого HTML-сообщения для Telegram."""

from __future__ import annotations

from html import escape
from typing import Any

CRM_LEAD_URL_TEMPLATE = "https://{portal}/crm/lead/details/{lead_id}/"
CRM_DEAL_URL_TEMPLATE = "https://{portal}/crm/deal/details/{deal_id}/"


def _extract_multifield_values(raw: Any) -> list[str]:
    """PHONE/EMAIL приходят как [{"VALUE": "...", "VALUE_TYPE": "..."}] либо пусто/None."""
    if not raw:
        return []
    values = []
    for item in raw:
        value = (item or {}).get("VALUE")
        if value:
            values.append(str(value))
    return values


def _build_full_name(lead: dict[str, Any]) -> str | None:
    parts = [lead.get("NAME"), lead.get("SECOND_NAME"), lead.get("LAST_NAME")]
    name = " ".join(p for p in parts if p)
    return name or None


def build_lead_notification(
    lead: dict[str, Any],
    *,
    portal_domain: str | None = None,
    source_name: str | None = None,
    assigned_name: str | None = None,
    is_junk: bool = False,
    duplicate_of_lead_id: str | None = None,
) -> str:
    """Собирает HTML-сообщение для sendMessage(parse_mode=HTML)."""

    lead_id = lead.get("ID", "")
    if duplicate_of_lead_id:
        lines: list[str] = ["⚠️ <b>Новый лид — Дубликат</b>"]
    elif is_junk:
        lines = ["🗑 <b>Отправлен на стадию «Мусор»</b>"]
    else:
        lines = ["🆕 <b>Новый лид</b>"]

    full_name = _build_full_name(lead)
    if full_name:
        lines.append(f"👤 {escape(full_name)}")

    phones = _extract_multifield_values(lead.get("PHONE"))
    if phones:
        phones_str = ", ".join(f"<code>{escape(p)}</code>" for p in phones)
        lines.append(f"📞 {phones_str}")

    emails = _extract_multifield_values(lead.get("EMAIL"))
    if emails:
        emails_str = ", ".join(f"<code>{escape(e)}</code>" for e in emails)
        lines.append(f"✉️ {emails_str}")

    source_label = source_name or lead.get("SOURCE_ID")
    if source_label:
        lines.append(f"🌐 Источник: {escape(str(source_label))}")

    source_description = lead.get("SOURCE_DESCRIPTION")
    if source_description and not str(source_description).startswith('manual-bot:'):
        lines.append(f"📝 {escape(source_description)}")

    utm_fields = {
        "utm_source": lead.get("UTM_SOURCE"),
        "utm_medium": lead.get("UTM_MEDIUM"),
        "utm_campaign": lead.get("UTM_CAMPAIGN"),
        "utm_content": lead.get("UTM_CONTENT"),
        "utm_term": lead.get("UTM_TERM"),
    }
    utm_pairs = [f"{key}={value}" for key, value in utm_fields.items() if value]
    if utm_pairs:
        lines.append(f"🏷 UTM: <code>{escape(', '.join(utm_pairs))}</code>")

    comments = lead.get("COMMENTS")
    if comments:
        lines.append(f"💬 {escape(comments)}")

    if assigned_name:
        lines.append(f"👔 Ответственный: {escape(assigned_name)}")

    if duplicate_of_lead_id:
        if portal_domain:
            dup_url = CRM_LEAD_URL_TEMPLATE.format(portal=portal_domain, lead_id=duplicate_of_lead_id)
            lines.append(f'🔁 Совпадает по телефону с лидом <a href="{escape(dup_url)}">№{escape(str(duplicate_of_lead_id))}</a>')
        else:
            lines.append(f"🔁 Совпадает по телефону с лидом №{escape(str(duplicate_of_lead_id))}")
        lines.append("🔀 Автоматически перенесён на стадию «Дубль»")

    if portal_domain and lead_id:
        crm_url = CRM_LEAD_URL_TEMPLATE.format(portal=portal_domain, lead_id=lead_id)
        lines.append(f'🔗 <a href="{escape(crm_url)}">Открыть лид в CRM</a>')

    return "\n".join(lines)


def _user_label(user: dict[str, Any]) -> str:
    return " ".join(p for p in (user.get("NAME"), user.get("LAST_NAME")) if p) or user.get("EMAIL") or str(user["ID"])


def build_manage_keyboard(lead_id: str | int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🛠 Управлять", "callback_data": f"m:{lead_id}"}],
        ]
    }


def build_deal_notification(deal: dict[str, Any], *, contact: dict[str, Any] | None = None,
                            portal_domain: str | None = None, source_name: str | None = None,
                            assigned_name: str | None = None, is_junk: bool = False,
                            related_lead_id: str | None = None,
                            relation_kind: str | None = None) -> str:
    lines = ["🗑 <b>Сделка отправлена на стадию «Мусор»</b>" if is_junk else "🆕 <b>Новая сделка</b>"]
    title = deal.get("TITLE")
    if title:
        lines.append(f"📋 {escape(str(title))}")
    if related_lead_id:
        if relation_kind == "exact_seen":
            relation_label = "Создана из ранее записанного лида"
        elif relation_kind == "exact":
            relation_label = "Создана из лида"
        else:
            relation_label = "Контакт совпадает с ранее записанным лидом"
        if portal_domain:
            lead_url = CRM_LEAD_URL_TEMPLATE.format(portal=portal_domain, lead_id=related_lead_id)
            lines.append(f'🔁 {relation_label}: <a href="{escape(lead_url)}">№{escape(str(related_lead_id))}</a>')
        else:
            lines.append(f"🔁 {relation_label}: №{escape(str(related_lead_id))}")
    if contact:
        full_name = _build_full_name(contact)
        if full_name:
            lines.append(f"👤 {escape(full_name)}")
        phones = _extract_multifield_values(contact.get("PHONE"))
        if phones:
            lines.append("📞 " + ", ".join(f"<code>{escape(phone)}</code>" for phone in phones))
        emails = _extract_multifield_values(contact.get("EMAIL"))
        if emails:
            lines.append("✉️ " + ", ".join(f"<code>{escape(email)}</code>" for email in emails))
    if source_name or deal.get("SOURCE_ID"):
        lines.append(f"🌐 Источник: {escape(str(source_name or deal['SOURCE_ID']))}")
    if deal.get("SOURCE_DESCRIPTION"):
        description = str(deal["SOURCE_DESCRIPTION"])
        lines.append(f"📝 {escape(description[:1600] + ('…' if len(description) > 1600 else ''))}")
    if deal.get("COMMENTS"):
        comments = str(deal["COMMENTS"])
        lines.append(f"💬 {escape(comments[:1000] + ('…' if len(comments) > 1000 else ''))}")
    if assigned_name:
        lines.append(f"👔 Ответственный: {escape(assigned_name)}")
    if portal_domain and deal.get("ID"):
        url = CRM_DEAL_URL_TEMPLATE.format(portal=portal_domain, deal_id=deal["ID"])
        lines.append(f'🔗 <a href="{escape(url)}">Открыть сделку в CRM</a>')
    return "\n".join(lines)


def build_deal_manage_keyboard(deal_id: str | int) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "🛠 Управлять", "callback_data": f"dm:{deal_id}"}]]}


def build_deal_action_keyboard(deal_id: str | int) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "👤 Назначить ответственного", "callback_data": f"da:{deal_id}"}],
        [{"text": "🗑 В мусор", "callback_data": f"dj:{deal_id}"}],
        [{"text": "⬅️ Назад", "callback_data": f"db:{deal_id}"}],
    ]}


def build_deal_assign_keyboard(deal_id: str | int, users: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for user in users:
        name = " ".join(p for p in (user.get("NAME"), user.get("LAST_NAME")) if p) or user.get("EMAIL") or user["ID"]
        rows.append([{"text": name, "callback_data": f"dau:{deal_id}:{user['ID']}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": f"db:{deal_id}"}])
    return {"inline_keyboard": rows}


def build_action_keyboard(lead_id: str | int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "👤 Назначить ответственного", "callback_data": f"a:{lead_id}"}],
            [{"text": "🗑 В мусор", "callback_data": f"j:{lead_id}"}],
            [{"text": "⬅️ Назад", "callback_data": f"b:{lead_id}"}],
        ]
    }


def build_assign_keyboard(lead_id: str | int, users: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [[{"text": _user_label(user), "callback_data": f"au:{lead_id}:{user['ID']}"}] for user in users]
    rows.append([{"text": "⬅️ Назад", "callback_data": f"b:{lead_id}"}])
    return {"inline_keyboard": rows}


def build_link_pick_keyboard(users: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [[{"text": _user_label(user), "callback_data": f"link_pick:{user['ID']}"}] for user in users]
    return {"inline_keyboard": rows}


def build_link_target_keyboard(bitrix_user_id: str, bitrix_name: str, starts: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for start in starts:
        label = f"{start['first_name'] or ''} (@{start['username']})" if start["username"] else f"{start['first_name'] or start['telegram_id']}"
        rows.append([{"text": label, "callback_data": f"link_to:{bitrix_user_id}:{start['telegram_id']}"}])
    rows.append([{"text": "⬅️ Отмена", "callback_data": "link_cancel"}])
    return {"inline_keyboard": rows}


def build_daily_report_body(
    leads: list[dict[str, Any]],
    *,
    portal_domain: str,
    source_map: dict[str, str],
    status_map: dict[str, str],
) -> str:
    """Группирует лиды одного менеджера: источник → стадия, каждая стадия отдельной строкой.

    Пример:
        <b>Телеграм</b>
        • «Мусор» — №1, №2
        • «Не обработан» — №3

        Всего: 3
    """
    by_source: dict[str, dict[str, list[str]]] = {}
    for lead in sorted(leads, key=lambda item: int(item["ID"])):
        lead_id = str(lead["ID"])
        source_name = source_map.get(str(lead.get("SOURCE_ID")), lead.get("SOURCE_ID") or "Без источника")
        status_name = status_map.get(str(lead.get("STATUS_ID")), lead.get("STATUS_ID") or "—")
        by_source.setdefault(source_name, {}).setdefault(status_name, []).append(lead_id)

    blocks = []
    for source_name, by_status in by_source.items():
        lines = [f"<b>{escape(str(source_name))}</b>"]
        for status_name, lead_ids in by_status.items():
            links = ", ".join(
                f'<a href="{escape(CRM_LEAD_URL_TEMPLATE.format(portal=portal_domain, lead_id=lead_id))}">№{escape(lead_id)}</a>'
                for lead_id in lead_ids
            )
            lines.append(f"• «{escape(str(status_name))}» — {links}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + f"\n\n<b>Всего: {len(leads)}</b>"
