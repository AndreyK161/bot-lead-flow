"""Telegram Mini App UI, authentication and live report API."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.period_reports import collect_live_period

router = APIRouter()
HTML_PATH = Path(__file__).with_name("static") / "miniapp.html"
CACHE_SECONDS = 300
MAX_AUTH_AGE_SECONDS = 86_400
_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


def validate_init_data(init_data: str, bot_token: str, *, now: int | None = None) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("missing hash")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("invalid hash")
    auth_date = int(values.get("auth_date") or 0)
    current = int(time.time()) if now is None else now
    if not auth_date or current - auth_date > MAX_AUTH_AGE_SECONDS or auth_date > current + 60:
        raise ValueError("expired auth data")
    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("missing user") from exc


def _authorized_user(request: Request) -> int:
    settings = get_settings()
    try:
        user = validate_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token,
        )
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Open this report from Telegram")
    if user_id not in settings.director_user_id_set:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return user_id


def _source_rows(
    items: list[dict], stage_key: str, entity_key: str, entity_type: str, portal_domain: str,
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["source_name"], []).append(item)
    result = []
    for source, source_items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        stage_groups: dict[str, list[dict]] = {}
        for item in source_items:
            stage_groups.setdefault(item[stage_key], []).append(item)
        result.append({
            "source": source,
            "total": len(source_items),
            "stages": [
                {
                    "name": name,
                    "count": len(stage_items),
                    "items": [
                        {
                            "id": item[entity_key],
                            "url": f"https://{portal_domain}/crm/{entity_type}/details/{item[entity_key]}/",
                            "manager_id": item["manager_id"],
                            "manager_name": item["manager_name"],
                            "converted": bool(item.get("converted")),
                            "direct": not bool(item.get("converted_from_report_lead")),
                            "contract": bool(item.get("contract")),
                            "deal_id": str(item.get("deal_id") or "") if entity_type == "lead" else "",
                            "deal_url": (
                                f"https://{portal_domain}/crm/deal/details/{item['deal_id']}/"
                                if entity_type == "lead" and item.get("deal_id") else ""
                            ),
                        }
                        for item in stage_items
                    ],
                }
                for name, stage_items in sorted(
                    stage_groups.items(), key=lambda pair: (-len(pair[1]), pair[0]),
                )
            ],
            "contracts": sum(bool(item.get("contract")) for item in source_items),
        })
    return result


def report_payload(data: dict) -> dict:
    leads = data["leads"]
    deals = data["deals"]
    managers = {
        (item["manager_id"], item["manager_name"])
        for item in [*leads, *deals]
    }
    return {
        "period": {
            "start": data["start"].isoformat(),
            "end": data["end"].isoformat(),
            "checked_at": data["checked_at"].isoformat(),
        },
        "totals": {
            "unique": data["unique_total"],
            "leads": len(leads),
            "direct_deals": data["direct_deal_count"],
            "converted": sum(bool(item["converted"]) for item in leads),
            "deals": len(deals),
            "contracts": sum(bool(item["contract"]) for item in deals),
        },
        "managers": [
            {"id": manager_id, "name": manager_name}
            for manager_id, manager_name in sorted(managers, key=lambda item: item[1])
        ],
        "leads": _source_rows(
            leads, "result_stage", "lead_id", "lead", data["portal_domain"],
        ),
        "deals": _source_rows(
            deals, "stage", "deal_id", "deal", data["portal_domain"],
        ),
    }


async def _cached_report(start: date, end: date) -> dict:
    key = (start, end)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    async with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        payload = report_payload(await collect_live_period(start, end))
        _cache[key] = (time.monotonic(), payload)
        return payload


@router.get("/miniapp", response_class=HTMLResponse)
async def mini_app_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@router.get("/api/miniapp/report")
async def mini_app_report(request: Request, start: date, end: date) -> dict:
    _authorized_user(request)
    if start > end:
        raise HTTPException(status_code=400, detail="Start date must not be after end date")
    if (end - start).days > 366:
        raise HTTPException(status_code=400, detail="Maximum period is 367 days")
    return await _cached_report(start, end)
