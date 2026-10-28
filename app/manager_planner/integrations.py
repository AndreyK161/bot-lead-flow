"""Read-only Bitrix and MegaFon integration for the manager Planner."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app import store
from app.bitrix_client import BitrixClient
from app.config import get_settings


class DeadlineOutcome(StrEnum):
    WORK_DONE_AND_MOVED = "work_done_and_moved"
    DEADLINE_ONLY = "deadline_only"


@dataclass(frozen=True)
class ExternalTask:
    external_id: str
    title: str
    url: str
    deadline: datetime | None
    external_status: str
    # Work completed today is independent of the external task's final status.
    work_done_today: bool = False
    deadline_outcome: DeadlineOutcome | None = None


class ReadOnlyCRM(Protocol):
    async def tasks(self, bitrix_user_id: int, day: str) -> list[ExternalTask]: ...
    async def call_statistics(self, bitrix_user_id: int, day: str, long_call_seconds: int = 180) -> dict: ...
    async def processed_leads(self, bitrix_user_id: int, day: str) -> dict: ...
    async def deals(self, bitrix_user_id: int, category_id: int) -> list[dict]: ...


def _value(item: dict, *keys: str):
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return None


def _datetime(value: object) -> datetime | None:
    if not value:
        return None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    return None


class PlannerCRM:
    """Adapter with no Bitrix mutation methods exposed to Planner code."""

    def __init__(self, client: BitrixClient | None = None) -> None:
        self.client = client or BitrixClient()

    async def tasks(self, bitrix_user_id: int, day: str) -> list[ExternalTask]:
        zone = ZoneInfo("Europe/Moscow")
        selected_day = date.fromisoformat(day)
        start = datetime.combine(selected_day, time.min, zone)
        end = start + timedelta(days=1)
        raw = await self.client.get_tasks_by_deadline(
            start.isoformat(), end.isoformat(), [str(bitrix_user_id)],
        )
        portal = urlparse(get_settings().bitrix_webhook_url).netloc
        result: list[ExternalTask] = []
        for item in raw:
            responsible = str(_value(item, "responsibleId", "RESPONSIBLE_ID") or "")
            if responsible != str(bitrix_user_id):
                continue
            task_id = str(_value(item, "id", "ID") or "")
            if not task_id:
                continue
            status = str(_value(item, "status", "STATUS") or "")
            closed_at = _datetime(_value(item, "closedDate", "CLOSED_DATE"))
            result.append(ExternalTask(
                external_id=f"bitrix:{task_id}",
                title=str(_value(item, "title", "TITLE") or f"Задача #{task_id}"),
                url=f"https://{portal}/company/personal/user/{bitrix_user_id}/tasks/task/view/{task_id}/",
                deadline=_datetime(_value(item, "deadline", "DEADLINE")),
                external_status=status,
                work_done_today=bool(closed_at and closed_at.astimezone(zone).date() == selected_day),
            ))
        return result

    async def call_statistics(
        self, bitrix_user_id: int, day: str, long_call_seconds: int = 180,
    ) -> dict:
        settings = get_settings()
        zone = ZoneInfo(settings.daily_stats_timezone)
        selected_day = date.fromisoformat(day)
        start = datetime.combine(selected_day, time.min, zone).astimezone(ZoneInfo("UTC"))
        end = (datetime.combine(selected_day, time.min, zone) + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
        extensions = [
            str(extension) for extension, user_id in settings.megafon_bitrix_users.items()
            if str(user_id) == str(bitrix_user_id)
        ]
        if not extensions:
            return {
                "configured": False, "total": 0, "outbound": 0, "answered": 0,
                "long_calls": 0, "duration_seconds": 0,
            }
        placeholders = ",".join("?" for _ in extensions)
        calls = store.rows(
            f"""SELECT direction,call_status,duration_seconds FROM megafon_calls
                WHERE manager_extension IN ({placeholders}) AND started_at>=? AND started_at<?""",
            (*extensions, start.isoformat(), end.isoformat()),
        )
        outgoing = [
            call for call in calls
            if str(call.get("direction") or "").casefold() in {"out", "outbound", "outgoing"}
        ]
        answered_statuses = {"success", "answered", "connected"}
        answered = [
            call for call in outgoing
            if str(call.get("call_status") or "").casefold() in answered_statuses
            or int(call.get("duration_seconds") or 0) > 0
        ]
        return {
            "configured": True,
            "total": len(calls),
            "outbound": len(outgoing),
            "answered": len(answered),
            "long_calls": sum(int(call.get("duration_seconds") or 0) >= long_call_seconds for call in answered),
            "duration_seconds": sum(int(call.get("duration_seconds") or 0) for call in answered),
        }

    async def deals(self, bitrix_user_id: int, category_id: int) -> list[dict]:
        return await self.client.get_user_deals(bitrix_user_id, category_id)

    async def processed_leads(self, bitrix_user_id: int, day: str) -> dict:
        """Count each lead once, on its first exit from the unprocessed stage."""
        timezone = ZoneInfo(get_settings().daily_stats_timezone)
        selected_day = date.fromisoformat(day)
        rows = store.rows(
            """SELECT entity_id,current_stage_id,current_stage_name,processed_at
               FROM crm_items
               WHERE entity_type='lead' AND processed_by_id=? AND processed_at IS NOT NULL""",
            (str(bitrix_user_id),),
        )
        selected = []
        for row in rows:
            processed_at = _datetime(row.get("processed_at"))
            if processed_at and processed_at.astimezone(timezone).date() == selected_day:
                selected.append(row)
        stages: dict[str, int] = {}
        for row in selected:
            stage = str(row.get("current_stage_name") or row.get("current_stage_id") or "Без стадии")
            stages[stage] = stages.get(stage, 0) + 1
        return {"processed": len(selected), "stages": stages}

    async def summary(self, bitrix_user_id: int, day: str) -> dict:
        calls, leads, category_zero, category_two = await asyncio.gather(
            self.call_statistics(bitrix_user_id, day),
            self.processed_leads(bitrix_user_id, day),
            self.deals(bitrix_user_id, 0),
            self.deals(bitrix_user_id, 2),
        )

        def pipeline(category_id: int, items: list[dict]) -> dict:
            stages: dict[str, int] = {}
            for item in items:
                stage = str(item.get("STAGE_ID") or "Без стадии")
                stages[stage] = stages.get(stage, 0) + 1
            return {
                "category_id": category_id,
                "total": len(items),
                "amount": sum(float(item.get("OPPORTUNITY") or 0) for item in items),
                "stages": stages,
            }

        return {
            "available": True,
            "day": day,
            "calls": calls,
            "leads": leads,
            "pipelines": [pipeline(0, category_zero), pipeline(2, category_two)],
        }


CRM_STATUS = {"connected": True, "message": "Bitrix24 и звонки МегаФона подключены в режиме чтения.",
              "deal_categories": [0, 2], "long_call_seconds": 180, "read_only": True}
