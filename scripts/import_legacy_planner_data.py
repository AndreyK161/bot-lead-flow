#!/usr/bin/env python3
"""Import one validated legacy Planner snapshot into Lead Flow.

The command is deliberately conservative: dry-run is the default, the source
is always opened read-only, and an apply is accepted only while all destination
business tables are empty.  The whole import is one SQLite transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path


SOURCE_VERSION = 4
TARGET_VERSION = 8
DATA_TABLES = (
    "plans",
    "tasks",
    "goal_requests",
    "audit",
    "reports",
    "distributions",
    "distribution_tasks",
    "distribution_leads",
    "system_audit",
    "import_attempts",
)
REQUIRED_TABLES = ("employees",) + DATA_TABLES
EMPLOYEE_REFERENCE_COLUMNS = {
    "plans": ("employee_id", "approved_by"),
    "tasks": ("employee_id", "created_by"),
    "goal_requests": ("employee_id", "decided_by"),
    "audit": ("actor_id",),
    "reports": ("closed_by",),
    "distribution_tasks": ("employee_id",),
    "distribution_leads": ("employee_id",),
}
JSON_COLUMNS = {
    "plans": ("initial_snapshot",),
    "audit": ("before_json", "after_json"),
    "reports": ("snapshot",),
    "system_audit": ("after_json",),
}
JSON_EMPLOYEE_KEYS = {"employee_id", "created_by", "approved_by", "closed_by", "actor_id", "decided_by"}


class ImportRejected(RuntimeError):
    pass


def readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def table_names(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))


def counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in REQUIRED_TABLES}


def rewrite_json(raw: str | None, employee_map: dict[int, int]) -> str | None:
    if raw is None:
        return None

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        rewritten = {key: walk(item) for key, item in value.items()}
        for key in JSON_EMPLOYEE_KEYS:
            old_id = rewritten.get(key)
            if isinstance(old_id, int) and old_id in employee_map:
                rewritten[key] = employee_map[old_id]
        # Employee audit snapshots store the primary key under the generic
        # `id` field, unlike plan/task snapshots which expose employee_id.
        if "role" in rewritten and ("telegram_id" in rewritten or "bitrix_id" in rewritten):
            old_id = rewritten.get("id")
            if isinstance(old_id, int) and old_id in employee_map:
                rewritten["id"] = employee_map[old_id]
        return rewritten

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ImportRejected("В исходной базе найдено повреждённое JSON-поле.") from error
    return json.dumps(walk(value), ensure_ascii=False, separators=(",", ":"))


def validate(source: sqlite3.Connection, target: sqlite3.Connection, source_hash: str) -> dict:
    if source.execute("PRAGMA user_version").fetchone()[0] != SOURCE_VERSION:
        raise ImportRejected(f"Ожидалась исходная схема {SOURCE_VERSION}.")
    if target.execute("PRAGMA user_version").fetchone()[0] != TARGET_VERSION:
        raise ImportRejected(f"Ожидалась целевая схема {TARGET_VERSION}.")
    for label, db in (("исходной", source), ("целевой", target)):
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ImportRejected(f"Нарушена целостность {label} базы: {integrity}")
    missing_source = set(REQUIRED_TABLES) - table_names(source)
    missing_target = set(REQUIRED_TABLES) - table_names(target)
    if missing_source or missing_target:
        raise ImportRejected(
            f"Не хватает таблиц: source={sorted(missing_source)}, target={sorted(missing_target)}"
        )
    for table in REQUIRED_TABLES:
        source_columns = columns(source, table)
        target_columns = columns(target, table)
        missing_columns = set(source_columns) - set(target_columns)
        if missing_columns:
            raise ImportRejected(f"В целевой таблице {table} нет колонок {sorted(missing_columns)}")
    prior = target.execute(
        "SELECT manifest_json FROM legacy_imports WHERE source_sha256=?", (source_hash,)
    ).fetchone()
    if prior:
        return {"status": "already_imported", "manifest": json.loads(prior[0])}
    occupied = {table: amount for table, amount in counts(target).items() if table != "employees" and amount}
    if occupied:
        raise ImportRejected(f"Целевая база уже содержит рабочие данные: {occupied}")
    return {"status": "ready", "source_counts": counts(source), "target_counts": counts(target)}


def map_employees(source: sqlite3.Connection, target: sqlite3.Connection, apply: bool) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for old in source.execute("SELECT * FROM employees ORDER BY id"):
        matches = target.execute(
            "SELECT * FROM employees WHERE (? IS NOT NULL AND telegram_id=?) "
            "OR (? IS NOT NULL AND bitrix_id=?)",
            (old["telegram_id"], old["telegram_id"], old["bitrix_id"], old["bitrix_id"]),
        ).fetchall()
        unique = {row["id"]: row for row in matches}
        if len(unique) > 1:
            raise ImportRejected(f"Привязки сотрудника {old['name']} указывают на разные записи.")
        if unique:
            current = next(iter(unique.values()))
            if current["role"] != old["role"]:
                raise ImportRejected(f"Роль сотрудника {old['name']} конфликтует с Lead Flow.")
            for key in ("telegram_id", "bitrix_id"):
                if old[key] is not None and current[key] != old[key]:
                    raise ImportRejected(f"Идентификатор сотрудника {old['name']} конфликтует с Lead Flow.")
            mapping[old["id"]] = current["id"]
        elif apply:
            cursor = target.execute(
                "INSERT INTO employees(telegram_id,bitrix_id,name,role,active) VALUES(?,?,?,?,?)",
                (old["telegram_id"], old["bitrix_id"], old["name"], old["role"], old["active"]),
            )
            mapping[old["id"]] = cursor.lastrowid
        else:
            # A negative placeholder makes dry-run validate every reference
            # without claiming a future SQLite id.
            mapping[old["id"]] = -old["id"]
    return mapping


def insert_row(db: sqlite3.Connection, table: str, row: dict) -> None:
    names = tuple(row)
    placeholders = ",".join("?" for _ in names)
    quoted = ",".join(f'"{name}"' for name in names)
    db.execute(f'INSERT INTO "{table}"({quoted}) VALUES({placeholders})', tuple(row[name] for name in names))


def transformed(row: sqlite3.Row, table: str, employee_map: dict[int, int]) -> dict:
    item = dict(row)
    for name in EMPLOYEE_REFERENCE_COLUMNS.get(table, ()):
        if item.get(name) is not None:
            try:
                item[name] = employee_map[item[name]]
            except KeyError as error:
                raise ImportRejected(f"Таблица {table} ссылается на неизвестного сотрудника.") from error
    for name in JSON_COLUMNS.get(table, ()):
        item[name] = rewrite_json(item.get(name), employee_map)
    return item


def run(source_path: Path, target_path: Path, apply: bool = False) -> dict:
    source_hash = digest(source_path)
    source = readonly(source_path)
    target = sqlite3.connect(target_path, timeout=10)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    try:
        target.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        state = validate(source, target, source_hash)
        if state["status"] == "already_imported":
            target.rollback()
            return state
        employee_map = map_employees(source, target, apply)
        if not apply:
            target.rollback()
            return {
                "status": "ready",
                "mode": "dry-run",
                "source_sha256": source_hash,
                "source_counts": state["source_counts"],
                "employee_matches": len(employee_map),
            }

        for row in source.execute("SELECT * FROM plans ORDER BY id"):
            insert_row(target, "plans", transformed(row, "plans", employee_map))

        task_links: list[tuple[int, int | None, int | None]] = []
        for row in source.execute("SELECT * FROM tasks ORDER BY id"):
            item = transformed(row, "tasks", employee_map)
            task_links.append((item["id"], item.pop("moved_to"), item.pop("source_task_id")))
            insert_row(target, "tasks", item)
        for task_id, moved_to, source_task_id in task_links:
            target.execute(
                "UPDATE tasks SET moved_to=?,source_task_id=? WHERE id=?",
                (moved_to, source_task_id, task_id),
            )

        for table in DATA_TABLES[2:]:
            for row in source.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                insert_row(target, table, transformed(row, table, employee_map))

        imported_counts = counts(target)
        for table, expected in state["source_counts"].items():
            if table != "employees" and imported_counts[table] != expected:
                raise ImportRejected(f"После импорта не совпало число строк в {table}.")
        foreign_key_errors = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ImportRejected(f"После импорта нарушены внешние ключи: {len(foreign_key_errors)}")
        manifest = {
            "format": 1,
            "source_sha256": source_hash,
            "source_schema": SOURCE_VERSION,
            "target_schema": TARGET_VERSION,
            "source_counts": state["source_counts"],
            "target_counts": imported_counts,
            "employee_map": {str(key): value for key, value in employee_map.items()},
        }
        target.execute(
            "INSERT INTO legacy_imports(source_sha256,imported_at,manifest_json) VALUES(?,?,?)",
            (source_hash, int(time.time()), json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))),
        )
        target.commit()
        return {"status": "imported", **manifest}
    except BaseException:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Read-only planner.sqlite3 snapshot")
    parser.add_argument("--target", type=Path, required=True, help="Lead Flow manager_planner.sqlite3")
    parser.add_argument("--apply", action="store_true", help="Commit the import; otherwise only validate")
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.target, args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
