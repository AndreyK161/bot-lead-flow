"""One-time, all-or-nothing migration from the legacy SQLite database."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import _initialize_postgres

TABLES = (
    "submissions",
    "sources",
    "source_updates",
    "deliveries",
    "telegram_starts",
    "manager_links",
    "deal_notifications",
    "deal_deliveries",
    "metadata",
    "seen_leads",
    "seen_lead_phones",
    "crm_items",
    "crm_item_events",
    "daily_report_deliveries",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="Path to the source SQLite database")
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL"),
        help="Destination PostgreSQL DSN (defaults to DATABASE_URL)",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    source = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source_tables = {
        row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    with psycopg.connect(args.database_url) as destination:
        _initialize_postgres(destination)
        destination.commit()

        nonempty = []
        for table in TABLES:
            count = destination.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            if count:
                nonempty.append(f"{table}={count}")
        if nonempty:
            raise SystemExit("PostgreSQL destination is not empty: " + ", ".join(nonempty))

        migrated: dict[str, int] = {}
        with destination.transaction():
            for table in TABLES:
                if table not in source_tables:
                    migrated[table] = 0
                    continue
                source_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                destination_columns = {
                    row[0] for row in destination.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    )
                }
                columns = [name for name in source_columns if name in destination_columns]
                if table == "crm_item_events":
                    columns = [name for name in columns if name != "id"]
                sqlite_columns = ",".join(f'"{name}"' for name in columns)
                records = source.execute(f'SELECT {sqlite_columns} FROM "{table}"').fetchall()
                if records:
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    )
                    with destination.cursor() as cursor:
                        cursor.executemany(statement, [tuple(row) for row in records])
                migrated[table] = len(records)

        for table, expected in migrated.items():
            actual = destination.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"Count mismatch for {table}: SQLite={expected}, PostgreSQL={actual}")
            print(f"{table}: {actual}")


if __name__ == "__main__":
    main()
