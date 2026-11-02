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
    tasks = [asyncio.create_task(sync.outbox_sync_loop(
        api.store, settings.distribution_confirmer_id_set, interval,
    ))]
    if legacy_path:
        tasks.append(asyncio.create_task(sync.sync_loop(
            api.store, Path(legacy_path), settings.legacy_distribution_confirmer_id_set,
            settings.legacy_distribution_poll_seconds,
        )))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await close_http_client()


app = FastAPI(title="Lead Flow Planner", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        with api.store.connection() as connection:
            connection.execute("SELECT 1 FROM plans LIMIT 1").fetchone()
        with core_store.db() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


app.mount("/manager", api.app)

