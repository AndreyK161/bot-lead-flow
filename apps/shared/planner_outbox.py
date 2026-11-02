"""Durable bot -> Planner messages in Lead Flow's existing PostgreSQL database.

SQLite is used only by the existing local/test fallback of the core store.
No Planner SQLite database is opened by these functions.
"""
import json

from apps.bot.backend import store
from .distributions import canonical, validate


def publish(snapshot, authorized_ids):
    digest = validate(snapshot)
    if snapshot["confirmed_by"] not in authorized_ids:
        raise PermissionError("Нет права подтверждать распределение.")
    with store.db() as connection:
        connection.execute(
            "INSERT INTO planner_distribution_outbox(distribution_id,workday,payload,sha256) "
            "VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
            (snapshot["distribution_id"], snapshot["workday"], canonical(snapshot), digest),
        )
        row = connection.execute(
            "SELECT * FROM planner_distribution_outbox WHERE distribution_id=? OR workday=?",
            (snapshot["distribution_id"], snapshot["workday"]),
        ).fetchone()
        if row is None or row["distribution_id"] != snapshot["distribution_id"]:
            raise ValueError("На этот день уже сохранено другое распределение.")
        if row["sha256"] != digest:
            # Retrying the same Telegram confirmation may have a later timestamp.
            # Preserve the original message; all business fields must still match.
            previous = json.loads(row["payload"])
            candidate = dict(snapshot)
            previous.pop("confirmed_at", None)
            candidate.pop("confirmed_at", None)
            if previous != candidate:
                raise ValueError("На этот день уже сохранено другое распределение.")
        return dict(row)


def pending():
    return store.rows(
        "SELECT * FROM planner_distribution_outbox WHERE status='pending' "
        "ORDER BY attempts,created_at,distribution_id LIMIT 100"
    )


def acknowledge(identifier, status):
    if status not in {"imported", "review"}:
        raise ValueError("Unknown Planner delivery status")
    store.execute(
        "UPDATE planner_distribution_outbox SET status=?,processed_at=CURRENT_TIMESTAMP,last_error='' "
        "WHERE distribution_id=? AND status='pending'", (status, identifier),
    )


def record_failure(identifier):
    store.execute(
        "UPDATE planner_distribution_outbox SET attempts=attempts+1,last_error='import_retry' "
        "WHERE distribution_id=? AND status='pending'", (identifier,),
    )
