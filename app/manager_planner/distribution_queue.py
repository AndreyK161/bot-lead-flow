"""Private, append-only bot outbox. No env loading, network, or planner writes."""
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
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


class DistributionQueue:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("Для очереди нужна отдельная база.")
                db.execute("CREATE TABLE confirmations(distribution_id TEXT PRIMARY KEY,workday TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,sha256 TEXT NOT NULL)")
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER immutable_{action.lower()} BEFORE {action} ON confirmations BEGIN SELECT RAISE(ABORT,'Immutable confirmation'); END")
                db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ValueError("Неизвестная версия очереди.")
            if [r[1] for r in db.execute("PRAGMA table_info(confirmations)")] != ["distribution_id", "workday", "payload", "sha256"]:
                raise ValueError("Файл не является очередью распределений.")
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise ValueError("Для read-only читателя нужен journal_mode=delete.")

    def publish(self, snapshot, authorized_ids):
        digest = validate(snapshot)
        # Explicit capability list, never all managers or all bot users.
        if snapshot["confirmed_by"] not in authorized_ids:
            raise PermissionError("Нет права подтверждать распределение.")
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT distribution_id,sha256 FROM confirmations WHERE workday=? OR distribution_id=?", (snapshot["workday"], snapshot["distribution_id"])).fetchone()
            if old:
                if old == (snapshot["distribution_id"], digest):
                    return False
                raise ValueError("На этот день уже сохранено другое распределение.")
            db.execute("INSERT INTO confirmations VALUES(?,?,?,?)", (snapshot["distribution_id"], snapshot["workday"], canonical(snapshot), digest))
        return True

    def read(self):
        # Never creates a missing DB; DELETE journal allows a read-only mount.
        db = sqlite3.connect(self.path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Неизвестная версия очереди.")
            for payload, digest in db.execute("SELECT payload,sha256 FROM confirmations ORDER BY workday"):
                snapshot = json.loads(payload)
                if validate(snapshot) != digest:
                    raise ValueError("Нарушена целостность очереди.")
                yield snapshot
        finally:
            db.close()

    def find(self, distribution_id):
        return next((item for item in self.read() if item["distribution_id"] == distribution_id), None)
