"""Тонкий клиент для чтения данных из Bitrix24 REST (входящий вебхук)."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings

# Поля, которые нужны для карточки лида. Явный список экономит трафик
# и не тянет лишнее из CRM.
LEAD_SELECT_FIELDS = [
    "ID",
    "TITLE",
    "NAME",
    "SECOND_NAME",
    "LAST_NAME",
    "PHONE",
    "EMAIL",
    "SOURCE_ID",
    "SOURCE_DESCRIPTION",
    "OPPORTUNITY",
    "CURRENCY_ID",
    "UTM_SOURCE",
    "UTM_MEDIUM",
    "UTM_CAMPAIGN",
    "UTM_CONTENT",
    "UTM_TERM",
    "COMMENTS",
    "ASSIGNED_BY_ID",
    "STATUS_ID",
]


class BitrixClient:
    def __init__(self, webhook_url: str | None = None) -> None:
        base = webhook_url or get_settings().bitrix_webhook_url
        self._base_url = base.rstrip("/") + "/"

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._base_url + method
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=params or {})
            response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise BitrixApiError(f"{method}: {payload.get('error')} — {payload.get('error_description')}")
        return payload["result"]

    async def get_lead(self, lead_id: str | int) -> dict[str, Any]:
        return await self._call("crm.lead.get", {"id": lead_id, "select": LEAD_SELECT_FIELDS})

    async def get_source_name(self, source_id: str) -> str | None:
        """Резолвит SOURCE_ID лида в человекочитаемое имя через справочник статусов."""
        if not source_id:
            return None
        result = await self._call(
            "crm.status.list",
            {"filter": {"ENTITY_ID": "SOURCE", "STATUS_ID": source_id}},
        )
        if result:
            return result[0].get("NAME")
        return None

    async def get_user_name(self, user_id: str) -> str | None:
        """Резолвит ASSIGNED_BY_ID в имя+фамилию ответственного."""
        if not user_id:
            return None
        result = await self._call("user.get", {"ID": user_id})
        if not result:
            return None
        user = result[0]
        full_name = " ".join(part for part in (user.get("NAME"), user.get("LAST_NAME")) if part)
        return full_name or user.get("EMAIL")

    async def get_department_users(self, department_id: str) -> list[dict[str, Any]]:
        """Активные сотрудники отдела — источник списка для назначения ответственного."""
        return await self._call(
            "user.get",
            {"FILTER": {"UF_DEPARTMENT": department_id, "ACTIVE": True}},
        )

    async def update_lead(self, lead_id: str | int, fields: dict[str, Any]) -> None:
        await self._call("crm.lead.update", {"id": lead_id, "fields": fields})

    async def move_to_junk(self, lead_id: str | int) -> dict[str, Any]:
        """Resolve the actual CRM stage and verify that the update took effect."""
        stages = await self._call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
        matches = [stage for stage in stages if str(stage.get("NAME", "")).strip().casefold() == "мусор"]
        if len(matches) != 1:
            raise BitrixApiError("Не найдена однозначная стадия «Мусор» в CRM")
        junk_id = str(matches[0]["STATUS_ID"])
        await self.update_lead(lead_id, {"STATUS_ID": junk_id})
        lead = await self.get_lead(lead_id)
        if str(lead.get("STATUS_ID")) != junk_id:
            raise BitrixApiError("Битрикс не подтвердил перенос на стадию «Мусор»")
        return lead

    async def add_lead(self, fields: dict[str, Any]) -> str:
        return str(await self._call("crm.lead.add", {"fields": fields}))

    async def get_sources(self) -> list[dict[str, Any]]:
        return await self._call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}, "order": {"SORT": "ASC"}})


class BitrixApiError(RuntimeError):
    """Битрикс вернул ошибку в теле ответа (200 OK, но {"error": ...})."""
