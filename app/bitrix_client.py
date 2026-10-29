"""Тонкий клиент для чтения данных из Bitrix24 REST (входящий вебхук)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

_shared_http_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class DistributionLead:
    id: int
    created_at: datetime
    stage_id: str


def _http_client() -> httpx.AsyncClient:
    """Reuse TLS connections across Bitrix REST calls and report pages."""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_http_client


async def close_http_client() -> None:
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        await _shared_http_client.aclose()
    _shared_http_client = None

# Поля, которые нужны для карточки лида. Явный список экономит трафик
# и не тянет лишнее из CRM.
LEAD_PREFERRED_CALL_TIME_FIELD = "UF_CRM_SELECTBOX"

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
    LEAD_PREFERRED_CALL_TIME_FIELD,
    "ASSIGNED_BY_ID",
    "CONTACT_ID",
    "STATUS_ID",
    "DATE_CREATE",
    "DATE_MODIFY",
]

DEAL_SELECT_FIELDS = [
    "ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "SOURCE_ID",
    "SOURCE_DESCRIPTION", "COMMENTS", "ASSIGNED_BY_ID", "CONTACT_ID",
    "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM", "LEAD_ID",
    "DATE_CREATE", "OPPORTUNITY", "CURRENCY_ID",
    "DATE_MODIFY",
]


class BitrixClient:
    def __init__(self, webhook_url: str | None = None) -> None:
        base = webhook_url or get_settings().bitrix_webhook_url
        self._base_url = base.rstrip("/") + "/"

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = await self._call_payload(method, params)
        return payload["result"]

    async def _call_payload(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._base_url + method
        try:
            response = await _http_client().post(url, json=params or {})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # httpx includes the complete request URL in its exception text. A Bitrix
            # incoming-webhook URL contains a credential, so never let that exception
            # reach application logs unchanged.
            raise BitrixApiError(
                f"{method}: Bitrix HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError as exc:
            raise BitrixTransportError(
                f"{method}: Bitrix transport error ({type(exc).__name__})"
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise BitrixApiError(f"{method}: Bitrix returned invalid JSON") from None
        if "error" in payload:
            raise BitrixApiError(f"{method}: {payload.get('error')} — {payload.get('error_description')}")
        return payload

    async def _call_all(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Read every Bitrix list page instead of silently stopping at the first 50 rows."""
        result: list[dict[str, Any]] = []
        start = 0
        while True:
            page_params = {**params, "start": start}
            payload = await self._call_payload(method, page_params)
            result.extend(payload.get("result") or [])
            if "next" not in payload:
                return result
            start = int(payload["next"])

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

    @staticmethod
    def _distribution_midnight(day: date, timezone: ZoneInfo) -> str:
        return datetime.combine(day, time.min, timezone).isoformat()

    async def _distribution_page(
        self,
        filters: dict[str, Any],
        *,
        stage_ids: Iterable[str],
        start: int = 0,
        newest_first: bool = False,
        timezone: ZoneInfo,
    ) -> tuple[list[DistributionLead], int | None]:
        stages = tuple(dict.fromkeys(str(value) for value in stage_ids if value))
        if not stages:
            raise ValueError("Выберите хотя бы одну настроенную стадию.")
        stage_filter = {"=stageId": stages[0]} if len(stages) == 1 else {"@stageId": list(stages)}
        payload = await self._call_payload("crm.item.list", {
            "entityTypeId": 1,
            "select": ["id", "createdTime", "stageId"],
            "filter": {**stage_filter, **filters},
            "order": {"createdTime": "DESC", "id": "DESC"} if newest_first else {"id": "ASC"},
            "start": start,
        })
        raw_items = payload.get("result", {}).get("items", [])
        leads: list[DistributionLead] = []
        for item in raw_items:
            created_at = datetime.fromisoformat(str(item["createdTime"]))
            if created_at.tzinfo is None:
                raise BitrixApiError("Bitrix returned a lead timestamp without timezone")
            lead = DistributionLead(int(item["id"]), created_at.astimezone(timezone), str(item["stageId"]))
            if lead.id <= 0 or lead.stage_id not in stages:
                raise BitrixApiError("Bitrix returned an invalid distribution lead")
            leads.append(lead)
        next_page = payload.get("next")
        return leads, int(next_page) if next_page is not None else None

    async def get_distribution_leads(
        self,
        first_day: date,
        last_day: date,
        stage_ids: Iterable[str],
        *,
        timezone: ZoneInfo,
    ) -> list[DistributionLead]:
        if first_day > last_day:
            raise ValueError("Invalid date range")
        filters = {
            ">=createdTime": self._distribution_midnight(first_day, timezone),
            "<createdTime": self._distribution_midnight(last_day + timedelta(days=1), timezone),
        }
        found: dict[int, DistributionLead] = {}
        start = 0
        while True:
            leads, next_page = await self._distribution_page(
                filters, stage_ids=stage_ids, start=start, timezone=timezone,
            )
            if next_page is not None and all(lead.id in found for lead in leads):
                raise BitrixApiError("Bitrix pagination made no progress")
            for lead in leads:
                if not first_day <= lead.created_at.date() <= last_day:
                    raise BitrixApiError("Bitrix returned a lead outside the requested range")
                found[lead.id] = lead
            if next_page is None:
                return list(found.values())
            start = next_page

    async def distribution_counts_by_stage(
        self,
        dates: Iterable[date],
        stage_ids: Iterable[str],
        *,
        timezone: ZoneInfo,
    ) -> dict[date, dict[str, int]]:
        days = sorted(set(dates))
        stages = tuple(dict.fromkeys(str(value) for value in stage_ids if value))
        counts = {day: dict.fromkeys(stages, 0) for day in days}
        if not days:
            return counts
        ranges: list[tuple[date, date]] = []
        first = last = days[0]
        for day in days[1:]:
            if day - last == timedelta(days=1):
                last = day
            else:
                ranges.append((first, last))
                first = last = day
        ranges.append((first, last))
        for first, last in ranges:
            for lead in await self.get_distribution_leads(first, last, stages, timezone=timezone):
                if lead.created_at.date() in counts:
                    counts[lead.created_at.date()][lead.stage_id] += 1
        return counts

    async def distribution_counts(
        self,
        dates: Iterable[date],
        stage_ids: Iterable[str],
        *,
        timezone: ZoneInfo,
    ) -> dict[date, int]:
        breakdown = await self.distribution_counts_by_stage(dates, stage_ids, timezone=timezone)
        return {day: sum(values.values()) for day, values in breakdown.items()}

    async def find_previous_distribution_day(
        self,
        before: date,
        stage_ids: Iterable[str],
        *,
        excluded: Iterable[date] = (),
        timezone: ZoneInfo,
    ) -> tuple[date, dict[str, int]] | None:
        excluded_days = set(excluded)
        stages = tuple(stage_ids)
        while True:
            leads, _ = await self._distribution_page(
                {"<createdTime": self._distribution_midnight(before, timezone)},
                stage_ids=stages,
                newest_first=True,
                timezone=timezone,
            )
            if not leads:
                return None
            day = max(lead.created_at.date() for lead in leads)
            if day >= before:
                raise BitrixApiError("Bitrix returned an invalid previous date")
            if day not in excluded_days:
                counts = (await self.distribution_counts_by_stage([day], stages, timezone=timezone))[day]
                if sum(counts.values()):
                    return day, counts
            before = day

    async def get_tasks_for_crm_items(self, bindings: list[str]) -> list[dict[str, Any]]:
        """Return Bitrix tasks bound to CRM items such as ``L_42`` or ``D_17``."""
        bindings = list(dict.fromkeys(str(value) for value in bindings if value))
        if not bindings:
            return []
        tasks: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = await self._call_payload("tasks.task.list", {
                "filter": {"UF_CRM_TASK": bindings},
                "select": [
                    "ID", "TITLE", "CREATED_DATE", "DEADLINE", "RESPONSIBLE_ID",
                    "CREATED_BY", "UF_CRM_TASK",
                ],
                "start": start,
            })
            result = payload.get("result") or {}
            tasks.extend((result.get("tasks") or []) if isinstance(result, dict) else result)
            if "next" not in payload:
                return tasks
            start = int(payload["next"])

    async def get_tasks_for_leads(self, lead_ids: list[str]) -> list[dict[str, Any]]:
        return await self.get_tasks_for_crm_items([f"L_{lead_id}" for lead_id in lead_ids])

    async def get_tasks_by_deadline(
        self, start_iso: str, end_iso: str, responsible_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return all tasks due in a period for the selected sales managers."""
        if not responsible_ids:
            return []
        tasks: list[dict[str, Any]] = []
        start = 0
        while True:
            payload = await self._call_payload("tasks.task.list", {
                "filter": {
                    ">=DEADLINE": start_iso,
                    "<DEADLINE": end_iso,
                    "RESPONSIBLE_ID": responsible_ids,
                },
                "order": {"DEADLINE": "ASC", "ID": "ASC"},
                "select": [
                    "ID", "TITLE", "CREATED_DATE", "DEADLINE", "CLOSED_DATE",
                    "STATUS", "RESPONSIBLE_ID", "CREATED_BY", "UF_CRM_TASK",
                ],
                "start": start,
            })
            result = payload.get("result") or {}
            tasks.extend((result.get("tasks") or []) if isinstance(result, dict) else result)
            if "next" not in payload:
                return tasks
            start = int(payload["next"])

    async def get_user_deals(
        self, user_id: str | int, category_id: str | int,
    ) -> list[dict[str, Any]]:
        """Return deals currently assigned to one employee in one pipeline."""
        return await self._call_all("crm.deal.list", {
            "order": {"ID": "DESC"},
            "filter": {
                "ASSIGNED_BY_ID": str(user_id),
                "CATEGORY_ID": str(category_id),
                "CLOSED": "N",
            },
            "select": [
                "ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "ASSIGNED_BY_ID",
                "OPPORTUNITY", "CURRENCY_ID", "DATE_CREATE", "DATE_MODIFY",
            ],
        })

    async def get_dialer_leads(
        self, user_id: str | int, start_iso: str, end_iso: str, stage_ids: list[str],
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {
            "ASSIGNED_BY_ID": str(user_id),
            ">=DATE_CREATE": start_iso,
            "<DATE_CREATE": end_iso,
        }
        if stage_ids:
            filters["STATUS_ID"] = list(dict.fromkeys(stage_ids))
        return await self._call_all("crm.lead.list", {
            "order": {"DATE_CREATE": "ASC", "ID": "ASC"},
            "filter": filters,
            "select": [
                "ID", "TITLE", "NAME", "SECOND_NAME", "LAST_NAME", "PHONE",
                "STATUS_ID", "SOURCE_ID", "ASSIGNED_BY_ID", "CONTACT_ID", "DATE_CREATE",
            ],
        })

    async def get_dialer_deals(
        self, user_id: str | int, category_id: str, start_iso: str, end_iso: str,
        stage_ids: list[str],
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {
            "ASSIGNED_BY_ID": str(user_id),
            "CATEGORY_ID": str(category_id),
            ">=DATE_CREATE": start_iso,
            "<DATE_CREATE": end_iso,
        }
        if stage_ids:
            filters["STAGE_ID"] = list(dict.fromkeys(stage_ids))
        return await self._call_all("crm.deal.list", {
            "order": {"DATE_CREATE": "ASC", "ID": "ASC"},
            "filter": filters,
            "select": [
                "ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "SOURCE_ID",
                "ASSIGNED_BY_ID", "CONTACT_ID", "DATE_CREATE",
            ],
        })

    async def add_crm_timeline_comment(
        self, entity_type: str, entity_id: str | int, comment: str,
    ) -> None:
        if entity_type not in {"lead", "deal"}:
            raise ValueError("Unsupported CRM entity type")
        await self._call("crm.timeline.comment.add", {
            "fields": {
                "ENTITY_ID": str(entity_id),
                "ENTITY_TYPE": entity_type,
                "COMMENT": comment,
            },
        })

    async def get_contacts_by_ids(self, contact_ids: list[str]) -> list[dict[str, Any]]:
        return await self._get_by_ids(
            "crm.contact.list", contact_ids, ["ID", "PHONE"],
        )

    async def get_contacts_full_by_ids(self, contact_ids: list[str]) -> list[dict[str, Any]]:
        """Contacts for client registries, including a display name and phone."""
        return await self._get_by_ids(
            "crm.contact.list",
            contact_ids,
            ["ID", "NAME", "SECOND_NAME", "LAST_NAME", "PHONE"],
        )

    async def update_lead(self, lead_id: str | int, fields: dict[str, Any]) -> None:
        await self._call("crm.lead.update", {"id": lead_id, "fields": fields})

    async def _move_lead_to_stage_named(self, lead_id: str | int, stage_name: str) -> dict[str, Any]:
        """Resolve the actual CRM stage by name and verify that the update took effect."""
        stages = await self._call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
        matches = [stage for stage in stages if str(stage.get("NAME", "")).strip().casefold() == stage_name.casefold()]
        if len(matches) != 1:
            raise BitrixApiError(f"Не найдена однозначная стадия «{stage_name}» в CRM")
        stage_id = str(matches[0]["STATUS_ID"])
        await self.update_lead(lead_id, {"STATUS_ID": stage_id})
        lead = await self.get_lead(lead_id)
        if str(lead.get("STATUS_ID")) != stage_id:
            raise BitrixApiError(f"Битрикс не подтвердил перенос на стадию «{stage_name}»")
        return lead

    async def move_to_junk(self, lead_id: str | int) -> dict[str, Any]:
        return await self._move_lead_to_stage_named(lead_id, "Мусор")

    async def move_to_duplicate_stage(self, lead_id: str | int) -> dict[str, Any]:
        return await self._move_lead_to_stage_named(lead_id, "Дубль")

    async def add_lead(self, fields: dict[str, Any]) -> str:
        return str(await self._call("crm.lead.add", {"fields": fields}))

    async def get_sources(self) -> list[dict[str, Any]]:
        return await self._call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}, "order": {"SORT": "ASC"}})

    async def get_lead_statuses(self) -> list[dict[str, Any]]:
        return await self._call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})

    async def get_deal_stages(self, category_id: str = "0") -> list[dict[str, Any]]:
        entity_id = "DEAL_STAGE" if str(category_id) == "0" else f"DEAL_STAGE_{category_id}"
        return await self._call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})

    async def get_leads_created_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return await self._call_all("crm.lead.list", {
            "filter": {">=DATE_CREATE": start_iso, "<DATE_CREATE": end_iso},
            "select": ["ID", "SOURCE_ID", "STATUS_ID", "ASSIGNED_BY_ID"],
        })

    async def get_leads_created_between_full(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return await self._call_all("crm.lead.list", {
            "order": {"DATE_CREATE": "ASC", "ID": "ASC"},
            "filter": {">=DATE_CREATE": start_iso, "<DATE_CREATE": end_iso},
            "select": LEAD_SELECT_FIELDS,
        })

    async def get_leads_modified_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        return await self._call_all("crm.lead.list", {
            "order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
            "filter": {">=DATE_MODIFY": start_iso, "<DATE_MODIFY": end_iso},
            "select": LEAD_SELECT_FIELDS,
        })

    async def get_deals_modified_between(
        self, category_id: str, start_iso: str, end_iso: str,
    ) -> list[dict[str, Any]]:
        return await self._call_all("crm.deal.list", {
            "order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
            "filter": {
                "CATEGORY_ID": str(category_id),
                ">=DATE_MODIFY": start_iso,
                "<DATE_MODIFY": end_iso,
            },
            "select": DEAL_SELECT_FIELDS,
        })

    async def get_deal(self, deal_id: str | int) -> dict[str, Any]:
        settings = get_settings()
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + [
            settings.deal_refusal_reason_field,
            settings.deal_contract_date_field,
        ]))
        return await self._call("crm.deal.get", {"id": deal_id, "select": select})

    async def get_deal_userfields(self) -> list[dict[str, Any]]:
        return await self._call_all(
            "crm.deal.userfield.list", {"order": {"ID": "ASC"}, "filter": {}},
        )

    async def _get_by_ids(
        self, method: str, ids: list[str], select: list[str], *, extra_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        unique_ids = list(dict.fromkeys(str(value) for value in ids if value))
        for offset in range(0, len(unique_ids), 50):
            item_filter: dict[str, Any] = {"ID": unique_ids[offset:offset + 50]}
            item_filter.update(extra_filter or {})
            result.extend(await self._call_all(method, {
                "order": {"ID": "ASC"}, "filter": item_filter, "select": select,
            }))
        return result

    async def get_leads_by_ids(self, lead_ids: list[str]) -> list[dict[str, Any]]:
        return await self._get_by_ids("crm.lead.list", lead_ids, LEAD_SELECT_FIELDS)

    async def get_deals_by_ids(
        self, deal_ids: list[str], *, select: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._get_by_ids("crm.deal.list", deal_ids, select or DEAL_SELECT_FIELDS)

    async def get_deals_by_lead_ids(self, lead_ids: list[str], category_id: str = "0") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        unique_ids = list(dict.fromkeys(str(value) for value in lead_ids if value))
        for offset in range(0, len(unique_ids), 50):
            result.extend(await self._call_all("crm.deal.list", {
                "order": {"ID": "ASC"},
                "filter": {
                    "CATEGORY_ID": str(category_id),
                    "LEAD_ID": unique_ids[offset:offset + 50],
                },
                "select": DEAL_SELECT_FIELDS,
            }))
        return result

    async def get_deals_by_contact_ids(
        self, contact_ids: list[str], category_id: str = "0", *, extra_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        unique_ids = list(dict.fromkeys(str(value) for value in contact_ids if value and str(value) != "0"))
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + list(extra_fields or [])))
        for offset in range(0, len(unique_ids), 50):
            result.extend(await self._call_all("crm.deal.list", {
                "order": {"ID": "ASC"},
                "filter": {
                    "CATEGORY_ID": str(category_id),
                    "CONTACT_ID": unique_ids[offset:offset + 50],
                },
                "select": select,
            }))
        return result

    async def get_deals_created_since(
        self, category_id: str, start_iso: str, *, extra_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + list(extra_fields or [])))
        return await self._call_all("crm.deal.list", {
            "order": {"DATE_CREATE": "ASC", "ID": "ASC"},
            "filter": {"CATEGORY_ID": str(category_id), ">=DATE_CREATE": start_iso},
            "select": select,
        })

    async def get_category_deals(
        self, category_id: str, *, extra_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the complete current contents of one deal pipeline."""
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + list(extra_fields or [])))
        return await self._call_all("crm.deal.list", {
            "order": {"ID": "DESC"},
            "filter": {"CATEGORY_ID": str(category_id)},
            "select": select,
        })

    async def get_deal_stage_history(self, deal_ids: list[str]) -> list[dict[str, Any]]:
        """Return stage transitions for deals, batching OWNER_ID filters."""
        rows: list[dict[str, Any]] = []
        unique_ids = list(dict.fromkeys(str(value) for value in deal_ids if value))
        for offset in range(0, len(unique_ids), 50):
            start = 0
            while True:
                payload = await self._call_payload("crm.stagehistory.list", {
                    "entityTypeId": 2,
                    "order": {"CREATED_TIME": "ASC", "ID": "ASC"},
                    "filter": {"OWNER_ID": unique_ids[offset:offset + 50]},
                    "select": ["ID", "OWNER_ID", "CREATED_TIME", "STAGE_ID", "CATEGORY_ID"],
                    "start": start,
                })
                result = payload.get("result") or {}
                rows.extend((result.get("items") or []) if isinstance(result, dict) else result)
                if "next" not in payload:
                    break
                start = int(payload["next"])
        return rows

    async def get_deals_created_between_full(
        self,
        category_id: str,
        start_iso: str,
        end_iso: str,
        *,
        extra_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + list(extra_fields or [])))
        return await self._call_all("crm.deal.list", {
            "order": {"DATE_CREATE": "ASC", "ID": "ASC"},
            "filter": {
                "CATEGORY_ID": str(category_id),
                ">=DATE_CREATE": start_iso,
                "<DATE_CREATE": end_iso,
            },
            "select": select,
        })

    async def get_category_deals_after_id(
        self, category_id: str, after_id: str | int, *, extra_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        select = list(dict.fromkeys(DEAL_SELECT_FIELDS + list(extra_fields or [])))
        return await self._call_all("crm.deal.list", {
            "order": {"ID": "ASC"},
            "filter": {"CATEGORY_ID": str(category_id), ">ID": int(after_id)},
            "select": select,
        })

    async def get_contact(self, contact_id: str | int) -> dict[str, Any]:
        return await self._call("crm.contact.get", {"id": contact_id})

    async def get_new_deals(self, category_id: str, after_id: str | int) -> list[dict[str, Any]]:
        return await self._call("crm.deal.list", {
            "order": {"ID": "ASC"},
            "filter": {">ID": int(after_id), "CATEGORY_ID": category_id},
            "select": DEAL_SELECT_FIELDS,
        })

    async def get_latest_deal_id(self, category_id: str) -> str:
        result = await self._call("crm.deal.list", {
            "order": {"ID": "DESC"}, "filter": {"CATEGORY_ID": category_id},
            "select": ["ID"], "start": 0,
        })
        return str(result[0]["ID"]) if result else "0"

    async def update_deal(self, deal_id: str | int, fields: dict[str, Any]) -> None:
        await self._call("crm.deal.update", {"id": deal_id, "fields": fields})

    async def find_duplicate_lead_ids(self, phones: list[str]) -> list[str]:
        """Использует встроенный поиск дублей Bitrix (crm.duplicate.findbycomm) по телефону."""
        values = [str(p) for p in phones if p]
        if not values:
            return []
        result = await self._call(
            "crm.duplicate.findbycomm",
            {"entity_type": "LEAD", "type": "PHONE", "values": values},
        )
        return [str(x) for x in (result or {}).get("LEAD", [])]

    async def get_active_lead_ids(self, lead_ids: list[str]) -> list[str]:
        """Из списка ID лидов оставляет только те, что ещё в работе (не мусор/не конвертированы)."""
        if not lead_ids:
            return []
        result = await self._call(
            "crm.lead.list",
            {"filter": {"ID": lead_ids}, "select": ["ID", "STATUS_SEMANTIC_ID"]},
        )
        return [str(item["ID"]) for item in result if item.get("STATUS_SEMANTIC_ID") == "P"]

    async def find_active_duplicate_lead(
        self,
        phones: list[str],
        *,
        exclude_lead_id: str | int | None = None,
        extra_candidate_ids: list[str] | None = None,
    ) -> str | None:
        """Ищет активный лид с тем же телефоном (кроме исключённого) — самый свежий по ID.

        Помимо встроенного поиска Bitrix (который не всегда считает +7/8-варианты одним номером),
        принимает extra_candidate_ids — кандидатов из локального кэша с честной нормализацией.
        """
        candidates = set(await self.find_duplicate_lead_ids(phones))
        candidates.update(str(c) for c in (extra_candidate_ids or []))
        if exclude_lead_id is not None:
            candidates.discard(str(exclude_lead_id))
        active = await self.get_active_lead_ids(list(candidates))
        return str(max(int(c) for c in active)) if active else None

    async def move_deal_to_junk(self, deal_id: str | int) -> dict[str, Any]:
        deal = await self.get_deal(deal_id)
        category_id = str(deal.get("CATEGORY_ID", "0"))
        entity_id = "DEAL_STAGE" if category_id == "0" else f"DEAL_STAGE_{category_id}"
        stages = await self._call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})
        matches = [stage for stage in stages if str(stage.get("NAME", "")).strip().casefold() == "мусор"]
        if len(matches) != 1:
            raise BitrixApiError("Не найдена однозначная стадия «Мусор» в воронке сделки")
        stage_id = str(matches[0]["STATUS_ID"])
        await self.update_deal(deal_id, {"STAGE_ID": stage_id})
        deal = await self.get_deal(deal_id)
        if str(deal.get("STAGE_ID")) != stage_id:
            raise BitrixApiError("Битрикс не подтвердил перенос сделки на стадию «Мусор»")
        return deal


class BitrixApiError(RuntimeError):
    """Bitrix rejected a request without exposing its credential-bearing URL."""


class BitrixTransportError(BitrixApiError):
    """A retryable network failure while contacting Bitrix."""
