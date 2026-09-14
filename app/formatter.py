"""Разбор полей лида Bitrix24 и сборка красивого HTML-сообщения для Telegram."""

from __future__ import annotations

from html import escape
from typing import Any

CRM_LEAD_URL_TEMPLATE = "https://{portal}/crm/lead/details/{lead_id}/"


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


def _format_amount(opportunity: Any, currency: str | None) -> str | None:
    if opportunity in (None, "", "0", 0):
        return None
    try:
        amount = float(opportunity)
    except (TypeError, ValueError):
        return None
    formatted = f"{amount:,.0f}".replace(",", " ")
    return f"{formatted} {currency}".strip() if currency else formatted


def build_lead_notification(
    lead: dict[str, Any],
    *,
    portal_domain: str | None = None,
    source_name: str | None = None,
    assigned_name: str | None = None,
) -> str:
    """Собирает HTML-сообщение для sendMessage(parse_mode=HTML)."""

    lead_id = lead.get("ID", "")
    lines: list[str] = ["🆕 <b>Новый лид</b>"]

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

    amount = _format_amount(lead.get("OPPORTUNITY"), lead.get("CURRENCY_ID"))
    if amount:
        lines.append(f"💰 {escape(amount)}")

    source_label = source_name or lead.get("SOURCE_ID")
    if source_label:
        lines.append(f"🌐 Источник: {escape(str(source_label))}")

    source_description = lead.get("SOURCE_DESCRIPTION")
    if source_description:
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

    if portal_domain and lead_id:
        crm_url = CRM_LEAD_URL_TEMPLATE.format(portal=portal_domain, lead_id=lead_id)
        lines.append(f'🔗 <a href="{escape(crm_url)}">Открыть лид в CRM</a>')

    return "\n".join(lines)


def build_manage_keyboard(lead_id: str | int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🛠 Управлять", "callback_data": f"m:{lead_id}"}],
        ]
    }


def build_action_keyboard(lead_id: str | int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "👤 Назначить ответственного", "callback_data": f"a:{lead_id}"}],
            [{"text": "🗑 В мусор", "callback_data": f"j:{lead_id}"}],
            [{"text": "⬅️ Назад", "callback_data": f"b:{lead_id}"}],
        ]
    }


def build_assign_keyboard(lead_id: str | int, users: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for user in users:
        name = " ".join(p for p in (user.get("NAME"), user.get("LAST_NAME")) if p) or user.get("EMAIL") or user["ID"]
        rows.append([{"text": name, "callback_data": f"au:{lead_id}:{user['ID']}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": f"b:{lead_id}"}])
    return {"inline_keyboard": rows}
