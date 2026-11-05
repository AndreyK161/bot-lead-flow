"""Redis/RQ-backed delivery for fire-and-forget Telegram notifications."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from redis import Redis
from rq import Queue, Retry

from apps.bot.backend.config import get_settings
from apps.bot.backend.telegram_client import send_telegram_message

logger = logging.getLogger(__name__)

# Long enough to outlive Bitrix journal retries and short enough not to retain
# successful job payloads indefinitely.
RESULT_TTL_SECONDS = 7 * 24 * 60 * 60
FAILURE_TTL_SECONDS = 30 * 24 * 60 * 60
RETRY_INTERVALS = [10, 30, 60, 180, 600, 1800, 3600, 7200]


def _connection() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def _queue() -> Queue:
    settings = get_settings()
    return Queue(settings.telegram_queue_name, connection=_connection())


def _job_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"telegram-{digest}"


def _enqueue(
    text: str,
    chat_id: int | str,
    reply_markup: dict[str, Any] | None,
    idempotency_key: str,
) -> str:
    queue = _queue()
    job_id = _job_id(idempotency_key)
    existing = queue.fetch_job(job_id)
    if existing is not None:
        return job_id

    settings = get_settings()
    max_retries = max(1, int(settings.telegram_queue_max_retries))
    intervals = RETRY_INTERVALS[:max_retries]
    if len(intervals) < max_retries:
        intervals.extend([RETRY_INTERVALS[-1]] * (max_retries - len(intervals)))
    queue.enqueue(
        deliver_telegram_message,
        text,
        str(chat_id),
        reply_markup,
        job_id=job_id,
        retry=Retry(max=max_retries, interval=intervals),
        result_ttl=RESULT_TTL_SECONDS,
        failure_ttl=FAILURE_TTL_SECONDS,
        job_timeout=120,
    )
    return job_id


async def enqueue_telegram_message(
    text: str,
    *,
    chat_id: int | str,
    reply_markup: dict[str, Any] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Persist a notification, falling back to direct delivery if Redis is down."""
    try:
        job_id = await asyncio.to_thread(
            _enqueue, text, chat_id, reply_markup, idempotency_key,
        )
        return {"queued": True, "job_id": job_id}
    except Exception:
        logger.exception(
            "Telegram queue unavailable; delivering directly chat_id=%s key=%s",
            chat_id, idempotency_key,
        )
        message = await send_telegram_message(
            text, chat_id=chat_id, reply_markup=reply_markup,
        )
        return {"queued": False, "message": message}


def deliver_telegram_message(
    text: str,
    chat_id: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """RQ worker entry point; exceptions trigger the configured retry policy."""
    return asyncio.run(send_telegram_message(
        text, chat_id=chat_id, reply_markup=reply_markup,
    ))


def health() -> dict[str, Any]:
    connection = _connection()
    connection.ping()
    queue = _queue()
    return {
        "status": "ok",
        "queued": queue.count,
        "failed": queue.failed_job_registry.count,
        "scheduled": queue.scheduled_job_registry.count,
    }
