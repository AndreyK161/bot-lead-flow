"""Telegram Mini App UI, authentication and live report API."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.period_reports import collect_live_period

router = APIRouter()
logger = logging.getLogger(__name__)
HTML_PATH = Path(__file__).with_name("static") / "miniapp.html"
CACHE_SECONDS = 300
REPORT_TIMEOUT_SECONDS = 900
MAX_AUTH_AGE_SECONDS = 86_400
_cache: dict[tuple[date, date], tuple[float, dict]] = {}
_cache_locks: dict[tuple[date, date], asyncio.Lock] = {}
_jobs: dict[str, dict] = {}
JOB_TTL_SECONDS = 900


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
    allowed_users = settings.director_user_id_set | settings.admin_user_id_set
    if user_id not in allowed_users:
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
        "managers": data["managers"],
        "leads": _source_rows(
            leads, "result_stage", "lead_id", "lead", data["portal_domain"],
        ),
        "deals": _source_rows(
            deals, "stage", "deal_id", "deal", data["portal_domain"],
        ),
    }


async def _cached_report(start: date, end: date, progress=None) -> dict:
    key = (start, end)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        if progress:
            progress(100, "Готово")
        return cached[1]
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    if lock.locked() and progress:
        progress(3, "Ожидаю уже запущенную сверку этого периода")
    async with lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            if progress:
                progress(100, "Готово")
            return cached[1]
        try:
            async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                payload = report_payload(
                    await collect_live_period(start, end, progress=progress),
                )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Слишком большой период: Bitrix не успел подготовить данные. Выберите меньший диапазон.",
            )
        _cache[key] = (time.monotonic(), payload)
        if progress:
            progress(100, "Готово")
        return payload


def _validate_period(start: date, end: date) -> None:
    if start > end:
        raise HTTPException(status_code=400, detail="Start date must not be after end date")
    if (end - start).days > 366:
        raise HTTPException(status_code=400, detail="Maximum period is 367 days")


def _cleanup_jobs() -> None:
    cutoff = time.monotonic() - JOB_TTL_SECONDS
    for job_id, job in list(_jobs.items()):
        if job["created_at"] < cutoff and job["status"] != "running":
            _jobs.pop(job_id, None)


async def _run_report_job(job_id: str, start: date, end: date) -> None:
    job = _jobs[job_id]

    def update(percent: int, message: str) -> None:
        job["progress"] = max(job["progress"], min(100, percent))
        job["message"] = message

    try:
        job["result"] = await _cached_report(start, end, update)
        job["status"] = "done"
        update(100, "Готово")
    except Exception as exc:
        logger.exception("Mini App report job failed job_id=%s", job_id)
        job["status"] = "error"
        job["message"] = exc.detail if isinstance(exc, HTTPException) else "Не удалось получить данные из Bitrix"


@router.get("/miniapp", response_class=HTMLResponse)
async def mini_app_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@router.get("/api/miniapp/report")
async def mini_app_report(request: Request, start: date, end: date) -> dict:
    _authorized_user(request)
    _validate_period(start, end)
    return await _cached_report(start, end)


@router.post("/api/miniapp/report/jobs")
async def start_mini_app_report_job(request: Request, start: date, end: date) -> dict:
    user_id = _authorized_user(request)
    _validate_period(start, end)
    _cleanup_jobs()
    for job_id, job in _jobs.items():
        if job["user_id"] == user_id and job["start"] == start and job["end"] == end and job["status"] == "running":
            return {"job_id": job_id}
    job_id = uuid4().hex
    job = {
        "user_id": user_id,
        "start": start,
        "end": end,
        "status": "running",
        "progress": 1,
        "message": "Готовлю запрос",
        "result": None,
        "created_at": time.monotonic(),
    }
    _jobs[job_id] = job
    job["task"] = asyncio.create_task(_run_report_job(job_id, start, end))
    return {"job_id": job_id}


@router.get("/api/miniapp/report/jobs/{job_id}")
async def mini_app_report_job(request: Request, job_id: str) -> dict:
    user_id = _authorized_user(request)
    job = _jobs.get(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Report job not found")
    response = {
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
    }
    if job["status"] == "done":
        response["result"] = job["result"]
    return response
