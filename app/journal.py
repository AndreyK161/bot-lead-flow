"""Durable journal for accepted Bitrix outgoing webhook events."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app import store

SECRET_KEYS = {"auth[application_token]", "auth[access_token]", "auth[refresh_token]"}


def _event_time(items: list[tuple[str, str]]) -> datetime | None:
    values = dict(items)
    raw = values.get("ts") or values.get("event_ts")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), timezone.utc)
    except (TypeError, ValueError, OSError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def begin(
    event_name: str,
    entity_type: str | None,
    entity_id: str | None,
    form_items: Iterable[tuple[object, object]],
) -> tuple[dict, bool]:
    """Persist before processing. Returns (row, already_processed)."""
    received_at = datetime.now(timezone.utc)
    sanitized = sorted(
        (str(key), str(value))
        for key, value in form_items
        if str(key).casefold() not in SECRET_KEYS and "token" not in str(key).casefold()
    )
    event_at = _event_time(sanitized)
    canonical = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    # Production Bitrix events include ts. Without it, do not collapse two
    # legitimate identical updates that happened at different times.
    hash_input = canonical if event_at else canonical + received_at.isoformat()
    payload_hash = hashlib.sha256(hash_input.encode()).hexdigest()
    payload_json = json.dumps(dict(sanitized), ensure_ascii=False, sort_keys=True)

    with store.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM bitrix_events WHERE payload_hash=?", (payload_hash,),
        ).fetchone()
        if existing and existing["status"] == "processed":
            return dict(existing), True
        if existing:
            conn.execute(
                "UPDATE bitrix_events SET status='pending',attempts=attempts+1,last_error=NULL "
                "WHERE id=?",
                (existing["id"],),
            )
        else:
            conn.execute(
                """INSERT INTO bitrix_events
                   (payload_hash,event_name,entity_type,entity_id,event_at,received_at,
                    payload_json,status,attempts)
                   VALUES (?,?,?,?,?,?,?,'pending',1)""",
                (
                    payload_hash, event_name, entity_type, entity_id,
                    event_at.isoformat() if event_at else None,
                    received_at.isoformat(), payload_json,
                ),
            )
        row = conn.execute(
            "SELECT * FROM bitrix_events WHERE payload_hash=?", (payload_hash,),
        ).fetchone()
        return dict(row), False


def mark_processed(event_id: int) -> None:
    store.execute(
        "UPDATE bitrix_events SET status='processed',processed_at=?,last_error=NULL WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), event_id),
    )


def mark_failed(event_id: int, error: BaseException) -> None:
    store.execute(
        "UPDATE bitrix_events SET status='failed',last_error=? WHERE id=?",
        (f"{type(error).__name__}: {error}"[:2000], event_id),
    )


def retryable(limit: int = 100) -> list[dict]:
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    return store.rows(
        "SELECT * FROM bitrix_events "
        "WHERE (status='failed' OR (status='pending' AND received_at<?)) AND attempts<10 "
        "ORDER BY received_at,id LIMIT ?",
        (stale_before, limit),
    )


def mark_retrying(event_id: int) -> None:
    store.execute(
        "UPDATE bitrix_events SET status='pending',attempts=attempts+1,last_error=NULL WHERE id=?",
        (event_id,),
    )
