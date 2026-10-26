"""Read-only outbox polling, isolated per record. No CRM calls or public auth."""
import asyncio
import json
import logging
import sqlite3
import time
from contextlib import closing

from .distribution_import import import_snapshot
from .distribution_queue import validate

logger = logging.getLogger(__name__)


def sync_once(store, queue_path, authorized_ids):
    with closing(sqlite3.connect(queue_path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unknown queue schema")
        rows = db.execute("SELECT distribution_id,workday,payload,sha256 FROM confirmations ORDER BY workday").fetchall()
    results = []
    for identifier, day, payload, digest in rows:
        try:
            with store.connection() as db:
                previous = db.execute("SELECT status FROM distributions WHERE id=? AND sha256=?", (identifier, digest)).fetchone()
            if previous:
                with store.connection(True) as db:
                    db.execute("INSERT INTO import_attempts VALUES(?,?,1,?,'') ON CONFLICT(id) DO UPDATE SET status=excluded.status,error_code='' WHERE import_attempts.status<>excluded.status", (identifier, previous[0], int(time.time())))
                continue
            snapshot = json.loads(payload)
            if snapshot["distribution_id"] != identifier or snapshot["workday"] != day or validate(snapshot) != digest:
                raise ValueError("Invalid queue record")
            result = import_snapshot(store, snapshot, authorized_ids)
            status, error = result["status"], ""
        except (ValueError, KeyError, TypeError, PermissionError, sqlite3.Error):
            status, error = "error", "record_failed"
            logger.warning("Distribution import record failed")
        with store.connection(True) as db:
            db.execute("INSERT INTO import_attempts VALUES(?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,attempts=attempts+1,last_attempt=excluded.last_attempt,error_code=excluded.error_code", (identifier, status, int(time.time()), error))
        results.append(status)
    return results


async def sync_loop(store, path, authorized_ids, interval=30):
    if interval <= 0:
        raise ValueError("Positive polling interval required")
    while True:
        try:
            await asyncio.to_thread(sync_once, store, path, authorized_ids)
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("Distribution queue temporarily unavailable")
        await asyncio.sleep(interval)


def distribution_notices(store, actor, employee_id=None):
    if actor["role"] != "supervisor":
        store.owner(actor, employee_id or actor["id"])
        employee_id = actor["id"]
    with store.connection() as db:
        rows = db.execute("SELECT d.id,d.workday,d.payload,d.reason,x.employee_id,x.status,e.name,e.telegram_id,e.bitrix_id FROM distributions d JOIN distribution_tasks x ON x.distribution_id=d.id JOIN employees e ON e.id=x.employee_id" + (" WHERE x.employee_id=?" if employee_id is not None else ""), (employee_id,) if employee_id is not None else ()).fetchall()
        result = []
        for row in rows:
            allocation = next((a for a in json.loads(row["payload"])["allocations"] if a["telegram_id"] == row["telegram_id"] and a["bitrix_id"] == row["bitrix_id"]), None)
            if allocation is None:
                continue  # Changed/conflicting identity: don't expose another person's data.
            reason = row["reason"]
            if row["status"] == "review" and not reason:
                reason = "plan_already_confirmed_or_closed"
            result.append({"id": row["id"], "day": row["workday"], "employee_id": row["employee_id"], "employee_name": row["name"], "status": row["status"], "reason": reason, "count": allocation["count"], "dates": allocation["dates"]})
        if actor["role"] == "supervisor":
            for row in db.execute("SELECT id,workday,reason,payload FROM distributions WHERE status='review'"):
                if sum(r['id'] == row['id'] for r in result) < len(json.loads(row['payload'])['allocations']):
                    result.append({"id": row["id"], "day": row["workday"], "status": "review", "reason": row["reason"], "employee_name": "Привязка сотрудников требует проверки", "dates": [], "count": None})
            for row in db.execute("SELECT id FROM import_attempts WHERE status='error'"):
                result.append({"id": row["id"], "status": "error", "reason": "import_retry", "employee_name": "Ошибка импорта — повторяем автоматически", "dates": [], "count": None})
        return result
