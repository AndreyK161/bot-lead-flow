"""Versioned distribution message contract; no application startup or storage."""
import hashlib
import json
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
BINDINGS = {8306778724: 460, 7939114214: 110, 7874883320: 50}


def canonical(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate(snapshot):
    try:
        if set(snapshot) != {"distribution_id", "schema", "stage_id", "confirmed_at", "workday", "confirmed_by", "variant_index", "variant_id", "allocations", "total"}:
            raise ValueError()
        UUID(snapshot["distribution_id"])
        if snapshot["schema"] != 1 or snapshot["stage_id"] != "IN_PROCESS":
            raise ValueError()
        stamp = datetime.fromisoformat(snapshot["confirmed_at"])
        if stamp.tzinfo is None or stamp.astimezone(MOSCOW).date().isoformat() != snapshot["workday"]:
            raise ValueError()
        if type(snapshot["confirmed_by"]) is not int or snapshot["confirmed_by"] <= 0:
            raise ValueError()
        if type(snapshot["variant_index"]) is not int or snapshot["variant_index"] < 0 or not snapshot["variant_id"]:
            raise ValueError()
        allocations = snapshot["allocations"]
        if not allocations or len(allocations) > 3:
            raise ValueError()
        employees, days, leads = set(), set(), set()
        for item in allocations:
            if set(item) != {"telegram_id", "bitrix_id", "name", "dates", "leads", "count"}:
                raise ValueError()
            tg = item["telegram_id"]
            if tg in employees or BINDINGS.get(tg) != item["bitrix_id"]:
                raise ValueError()
            employees.add(tg)
            assigned = item["dates"]
            if len(assigned) != len(set(assigned)) or not item["name"]:
                raise ValueError()
            for day in assigned:
                if date.fromisoformat(day).isoformat() != day or day in days or day >= snapshot["workday"]:
                    raise ValueError()
                days.add(day)
            for lead in item["leads"]:
                if set(lead) != {"id", "stage_id", "created_date"}:
                    raise ValueError()
                if type(lead["id"]) is not int or lead["id"] <= 0 or lead["id"] in leads:
                    raise ValueError()
                if lead["stage_id"] != "IN_PROCESS" or lead["created_date"] not in assigned:
                    raise ValueError()
                leads.add(lead["id"])
            if item["count"] != len(item["leads"]):
                raise ValueError()
        if snapshot["total"] != len(leads):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Некорректный снимок распределения.") from None
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()

