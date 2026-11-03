"""Standalone Planner website. Never starts Telegram/CRM/telephony bot workers."""
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apps.bot.backend import planner_access, store as core_store
from apps.bot.backend.bitrix_client import close_http_client
from apps.bot.backend.config import get_settings
from . import api, sync


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    interval = settings.planner_distribution_poll_seconds
    if interval <= 0:
        raise RuntimeError("PLANNER_DISTRIBUTION_POLL_SECONDS must be positive")
    legacy_path = settings.legacy_distribution_queue_path.strip()
    if legacy_path and not settings.legacy_distribution_confirmer_id_set:
        raise RuntimeError("LEGACY_DISTRIBUTION_CONFIRMER_IDS must be explicit")
    planner_access.seed_legacy_identities()
    api.initialize()
    tasks = {"outbox_sync": asyncio.create_task(sync.outbox_sync_loop(
        api.store, settings.distribution_confirmer_id_set, interval,
    ), name="outbox_sync")}
    if legacy_path:
        tasks["legacy_distribution_sync"] = asyncio.create_task(sync.sync_loop(
            api.store, Path(legacy_path), settings.legacy_distribution_confirmer_id_set,
            settings.legacy_distribution_poll_seconds,
        ), name="legacy_distribution_sync")
    app.state.background_tasks = tasks
    try:
        yield
    finally:
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with suppress(asyncio.CancelledError):
                await task
        await close_http_client()


app = FastAPI(title="Lead Flow Planner", lifespan=lifespan)


@app.get("/health")
def health():
    checks = {}
    healthy = True
    try:
        with api.store.connection() as connection:
            connection.execute("SELECT 1 FROM plans LIMIT 1").fetchone()
        with core_store.db() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        checks["database"] = "unavailable"
        healthy = False
    else:
        checks["database"] = "ok"
    tasks = getattr(app.state, "background_tasks", {})
    stopped = sorted(name for name, task in tasks.items() if task.done())
    checks["background_tasks"] = {"status": "ok" if not stopped else "stopped", "stopped": stopped}
    healthy = healthy and not stopped
    payload = {"status": "ok" if healthy else "unavailable", "checks": checks}
    return payload if healthy else JSONResponse(payload, status_code=503)


app.mount("/manager", api.app)

