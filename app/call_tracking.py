"""MegaFon call ingestion, lead matching and follow-up task tracking."""
from __future__ import annotations

import json
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status

from app import dialer, store
from app.bitrix_client import BitrixClient
from app.config import get_settings

router = APIRouter()
REJECTED_LEAD_STAGES = {"мусор", "брак", "дубль"}


def _phone(value: object) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _payload(body: bytes, content_type: str) -> dict:
    text = body.decode("utf-8", errors="replace")
    if "application/json" in content_type:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    return dict(parse_qsl(text, keep_blank_values=True))


def _parse_start(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            with suppress(ValueError):
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))
    return datetime.now(ZoneInfo("UTC"))


def save_history_event(data: dict) -> bool:
    """Persist only final history callbacks; event/contact callbacks are transient."""
    if str(data.get("cmd") or "").lower() != "history":
        return False
    call_id = str(data.get("callid") or "").strip()
    phone = _phone(data.get("phone"))
    if not call_id or not phone:
        raise ValueError("history callback requires callid and phone")
    try:
        duration = max(0, int(float(data.get("duration") or 0)))
        wait_seconds = max(0, int(float(
            data.get("wait") or data.get("waiting") or data.get("ringing_duration") or 0
        )))
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be numeric") from exc
    started_at = _parse_start(data.get("start")).isoformat()
    store.execute(
        """INSERT INTO megafon_calls
           (call_id,manager_extension,direction,client_phone,started_at,duration_seconds,
            wait_seconds,call_status,recording_url,processing_status)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(call_id) DO UPDATE SET
             manager_extension=excluded.manager_extension,direction=excluded.direction,
             client_phone=excluded.client_phone,started_at=excluded.started_at,
             duration_seconds=excluded.duration_seconds,wait_seconds=excluded.wait_seconds,
             call_status=excluded.call_status,
             recording_url=NULL,processing_status='not_applicable',
             updated_at=CURRENT_TIMESTAMP""",
        (
            call_id, str(data.get("ext") or data.get("user") or ""),
            str(data.get("type") or data.get("direction") or ""), phone,
            started_at, duration, wait_seconds, str(data.get("status") or ""), None, "not_applicable",
        ),
    )
    return True


@router.post("/megafon/webhook")
async def megafon_webhook(request: Request) -> dict[str, str]:
    settings = get_settings()
    body = await request.body()
    try:
        data = _payload(body, request.headers.get("content-type", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid callback body") from exc
    supplied = str(
        request.headers.get("X-CRM-AUTH")
        or request.headers.get("X-Megafon-Auth")
        or request.query_params.get("auth")
        or data.get("crm_token") or data.get("auth") or ""
    )
    if not settings.megafon_crm_token or supplied != settings.megafon_crm_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CRM token")
    try:
        dialer_handled = await dialer.process_callback(data)
        saved = save_history_event(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok" if saved or dialer_handled else "ignored"}


def _handoff_at(item: dict) -> datetime | None:
    events = store.rows(
        """SELECT occurred_at FROM crm_item_events
           WHERE entity_type=? AND entity_id=?
             AND event_type IN ('created','assignee_changed','transferred')
           ORDER BY occurred_at DESC,id DESC LIMIT 1""",
        (item["entity_type"], item["entity_id"]),
    )
    raw = events[0]["occurred_at"] if events else item.get("first_seen_at")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))
    return None


def _working_seconds_between(start: datetime, end: datetime, zone: ZoneInfo) -> int:
    """Elapsed time inside the daily 10:00–19:00 Moscow operating window."""
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    if local_end <= local_start:
        return 0
    total = 0.0
    day: date = local_start.date()
    while day <= local_end.date():
        opens = datetime.combine(day, time(10), tzinfo=zone)
        closes = datetime.combine(day, time(19), tzinfo=zone)
        overlap_start = max(local_start, opens)
        overlap_end = min(local_end, closes)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
        day += timedelta(days=1)
    return int(total)


def _tasks_by_item(tasks: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for task in tasks:
        bindings = task.get("ufCrmTask") or task.get("UF_CRM_TASK") or []
        if isinstance(bindings, str):
            bindings = [bindings]
        for binding in bindings:
            value = str(binding)
            if value.startswith(("L_", "D_")):
                entity_type = "lead" if value.startswith("L_") else "deal"
                grouped.setdefault((entity_type, value[2:]), []).append(task)
    return grouped


def _stage_path(
    item: dict, stage_names: dict[str, dict[str, str]], start: datetime, end: datetime,
) -> list[str]:
    events = store.rows(
        """SELECT new_value,occurred_at FROM crm_item_events
           WHERE entity_type=? AND entity_id=? AND event_type IN ('created','stage_changed')
           ORDER BY occurred_at,id""",
        (item["entity_type"], item["entity_id"]),
    )
    settings = get_settings()
    initial_stage = (
        getattr(settings, "lead_unprocessed_status_id", "NEW")
        if item["entity_type"] == "lead" else getattr(settings, "deal_unprocessed_stage_id", "NEW")
    )
    path: list[str] = []
    for event in events:
        occurred_at = _parse_datetime(event.get("occurred_at"))
        if not occurred_at or not (start <= occurred_at.astimezone(start.tzinfo) < end):
            continue
        stage_id = str(event.get("new_value") or "")
        if not stage_id or stage_id == str(initial_stage):
            continue
        name = stage_names[item["entity_type"]].get(stage_id) or stage_id
        if name.casefold().strip() == "не обработан":
            continue
        if not path or path[-1] != name:
            path.append(name)
    current_id = str(item.get("current_stage_id") or "")
    current_name = (
        stage_names[item["entity_type"]].get(current_id)
        or item.get("current_stage_name") or current_id or "Без стадии"
    )
    if not path and current_id != str(initial_stage) and current_name.casefold().strip() != "не обработан":
        path.append(str(current_name))
    return path


async def today_payload(*, now: datetime | None = None, client: BitrixClient | None = None) -> dict:
    zone = ZoneInfo(get_settings().daily_stats_timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    start = datetime.combine(current.date(), time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    items = store.rows(
        """SELECT * FROM crm_items WHERE entity_type IN ('lead','deal')
           AND created_date=? ORDER BY first_seen_at,entity_type,entity_id""",
        (current.date().isoformat(),),
    )
    client = client or BitrixClient()
    lead_statuses = await client.get_lead_statuses()
    deal_stages = await client.get_deal_stages(get_settings().track_deal_category_id)
    stage_names = {
        "lead": {str(row.get("STATUS_ID") or ""): str(row.get("NAME") or "") for row in lead_statuses},
        "deal": {str(row.get("STATUS_ID") or row.get("STAGE_ID") or ""): str(row.get("NAME") or "") for row in deal_stages},
    }
    deals = [item for item in items if item["entity_type"] == "deal"]
    live_deals = await client.get_deals_by_ids([item["entity_id"] for item in deals]) if deals else []
    live_deals_by_id = {str(deal["ID"]): deal for deal in live_deals}
    contact_ids = [str(deal.get("CONTACT_ID") or "") for deal in live_deals if deal.get("CONTACT_ID")]
    contacts = await client.get_contacts_by_ids(contact_ids) if contact_ids else []
    contacts_by_id = {str(contact["ID"]): contact for contact in contacts}
    bindings = [f"{'L' if item['entity_type'] == 'lead' else 'D'}_{item['entity_id']}" for item in items]
    tasks = await client.get_tasks_for_crm_items(bindings)
    tasks_by_item = _tasks_by_item(tasks)
    automation_user_ids = getattr(get_settings(), "bitrix_automation_user_id_set", {"24"})
    portal = get_settings().bitrix_webhook_url.split("/rest/", 1)[0]
    result = []
    for item in items:
        normalized: list[str] = []
        if item["entity_type"] == "lead":
            phones = store.rows("SELECT phone FROM seen_lead_phones WHERE lead_id=?", (item["entity_id"],))
            normalized.extend(row["phone"] for row in phones)
        else:
            live_deal = live_deals_by_id.get(str(item["entity_id"]), {})
            origin_lead_id = str(live_deal.get("LEAD_ID") or item.get("origin_lead_id") or "")
            if origin_lead_id and origin_lead_id != "0":
                phones = store.rows("SELECT phone FROM seen_lead_phones WHERE lead_id=?", (origin_lead_id,))
                normalized.extend(row["phone"] for row in phones)
            contact_id = str(live_deal.get("CONTACT_ID") or "")
            contact = contacts_by_id.get(contact_id, {})
            normalized.extend(
                _phone(phone.get("VALUE")) for phone in (contact.get("PHONE") or [])
                if _phone(phone.get("VALUE"))
            )
        normalized = list(dict.fromkeys(normalized))
        calls = []
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            calls = store.rows(
                f"""SELECT * FROM megafon_calls WHERE client_phone IN ({placeholders})
                    AND started_at>=? AND started_at<? AND direction='out'
                    ORDER BY started_at""",
                (*normalized, start.astimezone(ZoneInfo("UTC")).isoformat(), end.astimezone(ZoneInfo("UTC")).isoformat()),
            )
        handoff = _handoff_at(item)
        eligible = []
        user_map = getattr(get_settings(), "megafon_bitrix_users", {})
        for call in calls:
            when = datetime.fromisoformat(str(call["started_at"]).replace("Z", "+00:00"))
            mapped_manager = user_map.get(str(call["manager_extension"]))
            correct_manager = mapped_manager is None or mapped_manager == str(item.get("current_assignee_id") or "")
            if correct_manager and (handoff is None or when >= handoff):
                eligible.append((call, when))
        first = eligible[0] if eligible else None
        reaction = _working_seconds_between(handoff, first[1], zone) if first and handoff else None
        connected = next((pair[0] for pair in eligible if pair[0]["call_status"] == "Success"), None)
        chosen = connected or (first[0] if first else None)
        task = None
        if first:
            handled_at = first[1] + timedelta(seconds=int(first[0]["duration_seconds"] or 0))
            manager_id = str(item.get("current_assignee_id") or "")
            candidates = []
            for candidate in tasks_by_item.get((item["entity_type"], str(item["entity_id"])), []):
                created_at = _parse_datetime(candidate.get("createdDate") or candidate.get("CREATED_DATE"))
                responsible_id = str(candidate.get("responsibleId") or candidate.get("RESPONSIBLE_ID") or "")
                created_by = str(candidate.get("createdBy") or candidate.get("CREATED_BY") or "")
                if (
                    created_at and created_at >= handled_at
                    and manager_id in {responsible_id, created_by}
                    and created_by not in automation_user_ids
                ):
                    candidates.append((created_at, candidate))
            task = min(candidates, key=lambda item: item[0])[1] if candidates else None
        task_id = str((task or {}).get("id") or (task or {}).get("ID") or "")
        task_responsible = str((task or {}).get("responsibleId") or (task or {}).get("RESPONSIBLE_ID") or "")
        entity_type = str(item["entity_type"])
        entity_id = str(item["entity_id"])
        current_stage_name = (
            stage_names[entity_type].get(str(item.get("current_stage_id") or ""))
            or item.get("current_stage_name") or item.get("current_stage_id") or "Без стадии"
        )
        stage_path = _stage_path(item, stage_names, start, end)
        rejected = entity_type == "lead" and str(current_stage_name).casefold().strip() in REJECTED_LEAD_STAGES
        result.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_url": f"{portal}/crm/{entity_type}/details/{entity_id}/",
            "stage_id": item.get("current_stage_id") or "",
            "stage_name": current_stage_name,
            "stage_path": stage_path,
            "is_rejected": rejected,
            "manager_id": item.get("current_assignee_id") or "",
            "manager_name": item.get("current_assignee_name") or "Без ответственного",
            "handoff_at": handoff.isoformat() if handoff else None,
            "reaction_seconds": reaction,
            "call_count": len(eligible),
            "duration_seconds": int(chosen["duration_seconds"]) if chosen else None,
            "call_status": chosen["call_status"] if chosen else None,
            "task_title": (task or {}).get("title") or (task or {}).get("TITLE"),
            "task_deadline": (task or {}).get("deadline") or (task or {}).get("DEADLINE"),
            "task_created_at": (task or {}).get("createdDate") or (task or {}).get("CREATED_DATE"),
            "task_origin": "manual" if task else None,
            "task_url": (
                f"{portal}/company/personal/user/{task_responsible}/tasks/task/view/{task_id}/"
                if task_id and task_responsible else None
            ),
        })
    result.sort(key=lambda row: row["is_rejected"])
    return {"date": current.date().isoformat(), "items": result}
