"""Live Bitrix period audit and source-by-stage Excel reports."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from io import BytesIO
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app import outcomes, stats
from app.bitrix_client import BitrixClient
from app.config import get_settings
from app.telegram_client import send_telegram_document, send_telegram_message

logger = logging.getLogger(__name__)
_running: dict[int, asyncio.Task] = {}


def _iso_bounds(start: date, end: date, timezone_name: str) -> tuple[str, str]:
    timezone = ZoneInfo(timezone_name)
    start_at = datetime.combine(start, time.min, timezone)
    end_at = datetime.combine(end + timedelta(days=1), time.min, timezone)
    return start_at.isoformat(), end_at.isoformat()


def _stage_map(items: list[dict]) -> dict[str, str]:
    return {
        str(item.get("STATUS_ID") or ""): str(item.get("NAME") or item.get("STATUS_ID") or "Без стадии")
        for item in items
    }


def _after_or_same(item: dict, lead: dict) -> bool:
    item_date = stats._as_datetime(item.get("DATE_CREATE"))
    lead_date = stats._as_datetime(lead.get("DATE_CREATE"))
    return not item_date or not lead_date or item_date >= lead_date


async def collect_live_period(
    start: date,
    end: date,
    *,
    client: BitrixClient | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """Build a fresh lead cohort from Bitrix, independent of local tracking history."""
    settings = get_settings()
    client = client or BitrixClient()
    notify = progress or (lambda _percent, _message: None)
    notify(5, "Подключаюсь к Bitrix")
    start_iso, end_iso = _iso_bounds(start, end, settings.daily_stats_timezone)
    contract_field = settings.contract_source_url_field

    leads, sources, lead_statuses, sale_stages, contract_stages, sales_users = await asyncio.gather(
        client.get_leads_created_between_full(start_iso, end_iso),
        client.get_sources(),
        client.get_lead_statuses(),
        client.get_deal_stages(settings.track_deal_category_id),
        client.get_deal_stages(settings.accompaniment_deal_category_id),
        client.get_department_users(settings.sales_department_id),
    )
    notify(25, f"Загружено лидов: {len(leads)}")
    lead_ids = [str(lead["ID"]) for lead in leads]
    contact_ids = [str(lead.get("CONTACT_ID") or "") for lead in leads]
    explicit_deals, contact_deals, period_deals, contract_candidates = await asyncio.gather(
        client.get_deals_by_lead_ids(lead_ids, settings.track_deal_category_id),
        client.get_deals_by_contact_ids(contact_ids, settings.track_deal_category_id),
        client.get_deals_created_between_full(
            settings.track_deal_category_id, start_iso, end_iso,
        ),
        client.get_deals_created_since(
            settings.accompaniment_deal_category_id,
            start_iso,
            extra_fields=[contract_field],
        ),
    )
    notify(65, f"Загружены связанные сделки: {len(period_deals)}")

    source_names = _stage_map(sources)
    lead_names = _stage_map(lead_statuses)
    sale_names = _stage_map(sale_stages)
    contract_names = _stage_map(contract_stages)
    sales_by_id = {
        str(deal["ID"]): deal for deal in [*explicit_deals, *contact_deals, *period_deals]
    }
    lead_id_set = set(lead_ids)
    related_by_lead: dict[str, list[dict]] = {}
    for deal in sales_by_id.values():
        lead_id = str(deal.get("LEAD_ID") or "")
        if lead_id in lead_id_set:
            related_by_lead.setdefault(lead_id, []).append(deal)

    leads_by_contact: dict[str, list[dict]] = {}
    deals_by_contact: dict[str, list[dict]] = {}
    for lead in leads:
        contact_id = str(lead.get("CONTACT_ID") or "")
        if contact_id and contact_id != "0":
            leads_by_contact.setdefault(contact_id, []).append(lead)
    for deal in sales_by_id.values():
        contact_id = str(deal.get("CONTACT_ID") or "")
        if contact_id and contact_id != "0":
            deals_by_contact.setdefault(contact_id, []).append(deal)
    for contact_id, contact_leads in leads_by_contact.items():
        if len(contact_leads) != 1:
            continue
        lead = contact_leads[0]
        lead_id = str(lead["ID"])
        if not related_by_lead.get(lead_id):
            related_by_lead[lead_id] = [
                deal for deal in deals_by_contact.get(contact_id, []) if _after_or_same(deal, lead)
            ]

    contracts_by_source = outcomes._match_contracts(
        contract_candidates, list(sales_by_id.values()), contract_field,
    )
    notify(75, "Сопоставляю договоры и обращения")
    manager_ids = sorted({
        str(item.get("ASSIGNED_BY_ID") or "")
        for item in [*leads, *sales_by_id.values()]
        if str(item.get("ASSIGNED_BY_ID") or "")
    })
    manager_values = await asyncio.gather(*(client.get_user_name(user_id) for user_id in manager_ids))
    manager_names = {
        user_id: name or f"Сотрудник #{user_id}"
        for user_id, name in zip(manager_ids, manager_values)
    }
    sales_managers = [
        {
            "id": str(user["ID"]),
            "name": " ".join(
                part for part in (str(user.get("NAME") or ""), str(user.get("LAST_NAME") or ""))
                if part
            ) or str(user.get("EMAIL") or f"Сотрудник #{user['ID']}"),
        }
        for user in sales_users
    ]
    notify(85, "Группирую данные по ответственным")
    lead_stage_order = [lead_names[str(item.get("STATUS_ID") or "")] for item in lead_statuses]
    sale_stage_order = [sale_names[str(item.get("STATUS_ID") or "")] for item in sale_stages]
    lead_details: list[dict] = []
    selected_deals: dict[str, dict] = {}
    selected_lead_by_deal: dict[str, str] = {}
    lead_by_id = {str(lead["ID"]): lead for lead in leads}
    for lead in leads:
        lead_id = str(lead["ID"])
        deal = outcomes._pick_related_deal(
            related_by_lead.get(lead_id, []), None, contracts_by_source,
        )
        contract = contracts_by_source.get(str((deal or {}).get("ID") or ""))
        lead_stage_id = str(lead.get("STATUS_ID") or "")
        lead_result = "Сконвертирован" if deal else lead_names.get(
            lead_stage_id, lead_stage_id or "Без стадии",
        )
        source_id = str(lead.get("SOURCE_ID") or "")
        manager_id = str(lead.get("ASSIGNED_BY_ID") or "")
        lead_details.append({
            "lead_id": lead_id,
            "created_date": str(lead.get("DATE_CREATE") or "")[:10],
            "source_id": source_id,
            "source_name": source_names.get(source_id, source_id or "Без источника"),
            "lead_stage": lead_names.get(lead_stage_id, lead_stage_id or "Без стадии"),
            "result_stage": lead_result,
            "deal_id": str((deal or {}).get("ID") or ""),
            "deal_stage": sale_names.get(str((deal or {}).get("STAGE_ID") or ""), str((deal or {}).get("STAGE_ID") or "")),
            "contract_id": str((contract or {}).get("ID") or ""),
            "contract_stage": contract_names.get(
                str((contract or {}).get("STAGE_ID") or ""), str((contract or {}).get("STAGE_ID") or ""),
            ),
            "converted": bool(deal),
            "contract": bool(contract),
            "manager_id": manager_id,
            "manager_name": manager_names.get(manager_id, "Без ответственного"),
        })
        if deal:
            deal_id = str(deal["ID"])
            selected_deals[deal_id] = deal
            selected_lead_by_deal[deal_id] = lead_id

    for deal in period_deals:
        selected_deals[str(deal["ID"])] = deal

    deal_details: list[dict] = []
    for deal_id, deal in selected_deals.items():
        origin_lead_id = selected_lead_by_deal.get(deal_id)
        if not origin_lead_id:
            explicit_lead_id = str(deal.get("LEAD_ID") or "")
            if explicit_lead_id in lead_by_id:
                origin_lead_id = explicit_lead_id
        origin_lead = lead_by_id.get(origin_lead_id or "")
        contract = contracts_by_source.get(deal_id)
        source_id = str(deal.get("SOURCE_ID") or (origin_lead or {}).get("SOURCE_ID") or "")
        sale_stage_id = str(deal.get("STAGE_ID") or "")
        manager_id = str(deal.get("ASSIGNED_BY_ID") or "")
        deal_details.append({
            "deal_id": deal_id,
            "created_date": str(deal.get("DATE_CREATE") or "")[:10],
            "source_id": source_id,
            "source_name": source_names.get(source_id, source_id or "Без источника"),
            "stage": sale_names.get(sale_stage_id, sale_stage_id or "Без стадии"),
            "origin_lead_id": origin_lead_id or "",
            "converted_from_report_lead": bool(origin_lead_id),
            "contract_id": str((contract or {}).get("ID") or ""),
            "contract_stage": contract_names.get(
                str((contract or {}).get("STAGE_ID") or ""),
                str((contract or {}).get("STAGE_ID") or ""),
            ),
            "contract": bool(contract),
            "manager_id": manager_id,
            "manager_name": manager_names.get(manager_id, "Без ответственного"),
        })

    observed_lead_stages = {row["result_stage"] for row in lead_details}
    lead_columns = list(dict.fromkeys(
        stage for stage in [*lead_stage_order, "Сконвертирован"] if stage in observed_lead_stages
    ))
    lead_columns.extend(sorted(observed_lead_stages - set(lead_columns)))
    observed_sale_stages = {row["stage"] for row in deal_details}
    deal_columns = list(dict.fromkeys(
        stage for stage in sale_stage_order if stage in observed_sale_stages
    ))
    deal_columns.extend(sorted(observed_sale_stages - set(deal_columns)))
    direct_deal_count = sum(not item["converted_from_report_lead"] for item in deal_details)
    notify(96, "Формирую итоговую статистику")
    return {
        "start": start,
        "end": end,
        "leads": lead_details,
        "deals": deal_details,
        "lead_stage_columns": lead_columns,
        "deal_stage_columns": deal_columns,
        "direct_deal_count": direct_deal_count,
        "unique_total": len(lead_details) + direct_deal_count,
        "checked_at": datetime.now(ZoneInfo(settings.daily_stats_timezone)),
        "portal_domain": urlparse(settings.bitrix_webhook_url).netloc,
        "managers": sorted(sales_managers, key=lambda item: item["name"]),
    }


def _excel_text(value: object) -> str:
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _style_header(row) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for cell in row:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _add_matrix_sheet(
    workbook: Workbook,
    title: str,
    items: list[dict],
    stage_columns: list[str],
    stage_key: str,
    total_label: str,
    *,
    include_contracts: bool = False,
) -> None:
    sheet = workbook.create_sheet(title)
    headers = ["Источник", *stage_columns]
    if include_contracts:
        headers.append("Договоры")
    headers.append(total_label)
    sheet.append(headers)
    _style_header(sheet[1])
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["source_name"], []).append(item)
    for source, source_items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        counts = Counter(item[stage_key] for item in source_items)
        row = [_excel_text(source), *(counts.get(stage, 0) for stage in stage_columns)]
        if include_contracts:
            row.append(sum(item["contract"] for item in source_items))
        row.append(len(source_items))
        sheet.append(row)
    totals = Counter(item[stage_key] for item in items)
    total_row = ["ИТОГО", *(totals.get(stage, 0) for stage in stage_columns)]
    if include_contracts:
        total_row.append(sum(item["contract"] for item in items))
    total_row.append(len(items))
    sheet.append(total_row)
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, header in enumerate(headers, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(28, max(11, len(header) + 2))


def build_workbook(data: dict) -> bytes:
    workbook = Workbook()
    totals = workbook.active
    totals.title = "Итоги"
    lead_count = len(data["leads"])
    converted_count = sum(item["converted"] for item in data["leads"])
    deal_count = len(data["deals"])
    contract_count = sum(item["contract"] for item in data["deals"])
    totals.append(["Показатель", "Количество"])
    _style_header(totals[1])
    for label, value in (
        ("Всего обращений без дублей", data["unique_total"]),
        ("Лидов", lead_count),
        ("Прямых сделок", data["direct_deal_count"]),
        ("Лидов сконвертировано", converted_count),
        ("Всего сделок", deal_count),
        ("Договоров", contract_count),
    ):
        totals.append([label, value])
    totals.append([])
    totals.append([
        "Правило подсчёта",
        "Всего обращений = лиды + прямые сделки. Сделка из лида повторно в общий итог не входит.",
    ])
    totals.column_dimensions["A"].width = 34
    totals.column_dimensions["B"].width = 90
    totals["B9"].alignment = Alignment(wrap_text=True)

    _add_matrix_sheet(
        workbook, "Лиды", data["leads"], data["lead_stage_columns"], "result_stage", "Всего лидов",
    )
    _add_matrix_sheet(
        workbook, "Сделки", data["deals"], data["deal_stage_columns"], "stage", "Всего сделок",
        include_contracts=True,
    )

    detail = workbook.create_sheet("Детализация")
    detail_headers = [
        "Тип", "Дата создания", "ID", "Источник", "Стадия/результат",
        "Исходный лид", "Связанная сделка", "Договор", "Стадия сопровождения",
    ]
    detail.append(detail_headers)
    _style_header(detail[1])
    portal = data["portal_domain"]
    for item in data["leads"]:
        detail.append([
            "Лид", item["created_date"], item["lead_id"], _excel_text(item["source_name"]),
            _excel_text(item["result_stage"]), item["lead_id"], item["deal_id"],
            item["contract_id"], _excel_text(item["contract_stage"]),
        ])
    for item in data["deals"]:
        detail.append([
            "Сделка", item["created_date"], item["deal_id"], _excel_text(item["source_name"]),
            _excel_text(item["stage"]), item["origin_lead_id"], item["deal_id"],
            item["contract_id"], _excel_text(item["contract_stage"]),
        ])
    for row in range(2, detail.max_row + 1):
        entity_type = detail.cell(row, 1).value
        entity_id = detail.cell(row, 3).value
        path = "lead" if entity_type == "Лид" else "deal"
        detail.cell(row, 3).hyperlink = f"https://{portal}/crm/{path}/details/{entity_id}/"
        detail.cell(row, 3).style = "Hyperlink"
        for column, linked_path in ((6, "lead"), (7, "deal"), (8, "deal")):
            linked_id = detail.cell(row, column).value
            if linked_id:
                detail.cell(row, column).hyperlink = f"https://{portal}/crm/{linked_path}/details/{linked_id}/"
                detail.cell(row, column).style = "Hyperlink"
    detail.freeze_panes = "A2"
    detail.auto_filter.ref = detail.dimensions
    widths = [12, 15, 12, 28, 30, 14, 16, 14, 30]
    for index, width in enumerate(widths, start=1):
        detail.column_dimensions[get_column_letter(index)].width = width

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_caption(data: dict) -> str:
    leads = data["leads"]
    deals = data["deals"]
    converted = sum(item["converted"] for item in leads)
    contracts = sum(item["contract"] for item in deals)
    lead_total = len(leads)
    converted_rate = converted / lead_total * 100 if lead_total else 0
    contract_rate = contracts / data["unique_total"] * 100 if data["unique_total"] else 0
    return (
        f"📊 <b>{data['start']:%d.%m.%Y}–{data['end']:%d.%m.%Y}</b>\n"
        f"Обращений без дублей: <b>{data['unique_total']}</b>\n"
        f"Лидов: <b>{lead_total}</b> · прямых сделок: <b>{data['direct_deal_count']}</b>\n"
        f"Перешли в сделку: <b>{converted}</b> ({converted_rate:.1f}%)\n"
        f"Всего сделок: <b>{len(deals)}</b>\n"
        f"Договоры: <b>{contracts}</b> ({contract_rate:.1f}%)\n"
        f"Источников: <b>{len({item['source_name'] for item in [*leads, *deals]})}</b>\n"
        f"<i>Сверено с Bitrix: {data['checked_at']:%d.%m.%Y %H:%M}</i>"
    )


async def deliver_period_report(start: date, end: date, chat_id: int) -> None:
    try:
        data = await collect_live_period(start, end)
        content = build_workbook(data)
        filename = f"leads_{start:%d.%m.%Y}-{end:%d.%m.%Y}.xlsx"
        await send_telegram_document(
            content, filename, chat_id=chat_id, caption=build_caption(data),
        )
    except Exception:
        logger.exception("Live period report failed chat_id=%s start=%s end=%s", chat_id, start, end)
        try:
            await send_telegram_message(
                "Не удалось сверить отчёт с Bitrix. Ошибка записана в журнал, попробуйте позже.",
                chat_id=chat_id,
            )
        except Exception:
            logger.exception("Failed to notify chat_id=%s about period report error", chat_id)


def enqueue_period_report(start: date, end: date, chat_id: int) -> bool:
    existing = _running.get(chat_id)
    if existing and not existing.done():
        return False
    task = asyncio.create_task(deliver_period_report(start, end, chat_id))
    _running[chat_id] = task
    task.add_done_callback(lambda finished, key=chat_id: _running.pop(key, None))
    return True
