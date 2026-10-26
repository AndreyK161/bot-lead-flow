#!/usr/bin/env python3
"""Create validated, read-only snapshots of every legacy Planner database.

The source files are opened in SQLite read-only mode. Snapshots are written to
a new timestamped directory, so the running legacy bot and previous migration
runs are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATABASES = {
    "planner": {
        "tables": ("employees", "plans", "tasks", "goal_requests", "audit", "reports"),
        "count": ("employees", "plans", "tasks", "goal_requests", "audit", "reports"),
    },
    "access": {
        "tables": ("allowed_users", "reminders"),
        "count": ("allowed_users", "reminders"),
    },
    "queue": {
        "tables": ("confirmations",),
        "count": ("confirmations",),
    },
}


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def snapshot(kind: str, source_path: Path, destination: Path) -> dict:
    specification = DATABASES[kind]
    if not source_path.is_file():
        raise FileNotFoundError(f"Missing {kind} source: {source_path}")
    partial = destination.with_suffix(destination.suffix + ".partial")
    if destination.exists() or partial.exists():
        raise FileExistsError(f"Migration target already exists: {destination}")
    source = _readonly(source_path)
    try:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(specification["tables"]) - tables
        if missing:
            raise RuntimeError(f"{kind} source is missing tables: {sorted(missing)}")
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    check = sqlite3.connect(partial)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"{kind} snapshot failed integrity_check: {integrity}")
        counts = {
            table: check.execute(f'SELECT count(1) FROM "{table}"').fetchone()[0]
            for table in specification["count"]
        }
        schema_version = check.execute("PRAGMA user_version").fetchone()[0]
    finally:
        check.close()
    partial.replace(destination)
    destination.chmod(0o600)
    return {
        "file": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": _digest(destination),
        "schema_version": schema_version,
        "counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--planner", type=Path, required=True)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_directory = args.destination / stamp
    run_directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    sources = {"planner": args.planner, "access": args.access, "queue": args.queue}
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "sqlite-read-only-online-backup",
        "databases": {},
    }
    for kind, source_path in sources.items():
        manifest["databases"][kind] = snapshot(kind, source_path, run_directory / f"{kind}.sqlite3")
    manifest_path = run_directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    print(json.dumps({"directory": str(run_directory), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
