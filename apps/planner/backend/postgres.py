"""Opt-in PostgreSQL storage for Planner, isolated from the bot's public schema.

Write transactions take one transaction-scoped advisory lock before reading any
business state. This preserves the existing SQLite BEGIN IMMEDIATE semantics:
read/check/write operations (revision checks, timers, imports) remain atomic.
Read transactions use a consistent snapshot and cannot write.
"""
from contextlib import contextmanager
from pathlib import Path
import time

import psycopg
from psycopg.rows import dict_row

from .store import Store, SCHEMA_VERSION, EMPLOYEE_BINDINGS, EMPLOYEE_ALIASES, encode

WRITE_LOCK = 718245019604  # Separate from the bot schema initialization lock.
EXPECTED_TABLES = {
    "employees", "plans", "tasks", "goal_requests", "audit", "reports",
    "distributions", "distribution_tasks", "distribution_leads", "system_audit",
    "schema_migrations", "import_attempts", "legacy_imports", "employee_aliases",
    "storage_metadata",
}


class _Queries:
    """Bind internal qmark SQL to psycopg; parameter values are never rewritten."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, params=()):
        if not params:
            return self.connection.execute(statement)
        # Internal Planner queries have no literal question marks. Escape SQL
        # percent signs for psycopg; user strings travel only as bound values.
        statement = statement.replace("%", "%%").replace("?", "%s")
        params = tuple(int(value) if isinstance(value, bool) else value for value in params)
        return self.connection.execute(statement, params)


class PostgresStore(Store):
    previous_workday_sql = (
        "SELECT id FROM plans WHERE employee_id=? AND day<? "
        "AND EXTRACT(ISODOW FROM CAST(day AS date)) < 6 ORDER BY day DESC LIMIT 1"
    )

    def __init__(self, database_url: str):
        # An explicit invalid DSN must fail; never fall back to a different DB.
        self.database_url = database_url
        self.path = None

    @contextmanager
    def connection(self, write=False):
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            if write:
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
            else:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL search_path TO planner, pg_catalog")
            conn.execute("SET LOCAL lock_timeout = '10s'")
            conn.execute("SET LOCAL statement_timeout = '30s'")
            if write:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            yield _Queries(conn)

    @staticmethod
    def insert_id(db, statement, args=()):
        return db.execute(statement + " RETURNING id", args).fetchone()["id"]

    @staticmethod
    def schema_version(db):
        row = db.execute("SELECT version FROM storage_metadata WHERE singleton=1 AND application='leadflow-planner'").fetchone()
        if row is None:
            raise RuntimeError("Planner storage version is missing.")
        return row["version"]

    def initialize(self):
        with self.connection(True) as db:
            db.execute("CREATE SCHEMA IF NOT EXISTS planner")
            relations = {row["relname"] for row in db.execute(
                "SELECT c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='planner' AND c.relkind IN ('r','p','v','m','f')"
            )}
            if relations:
                if relations != EXPECTED_TABLES:
                    raise RuntimeError("Planner requires an empty, dedicated PostgreSQL schema.")
                if self.schema_version(db) != SCHEMA_VERSION:
                    raise RuntimeError("Unsupported Planner PostgreSQL schema version.")
                return  # Never reseed or overwrite saved employee bindings.
            db.execute(Path(__file__).with_name("postgres_schema.sql").read_text())
            for binding in EMPLOYEE_BINDINGS:
                db.execute("INSERT INTO employees(telegram_id,bitrix_id,name,role) VALUES(?,?,?,?)", binding)
            for telegram_id, bitrix_id in EMPLOYEE_ALIASES:
                db.execute(
                    "INSERT INTO employee_aliases(telegram_id,employee_id) "
                    "SELECT ?,id FROM employees WHERE bitrix_id=? AND role='manager'",
                    (telegram_id, bitrix_id),
                )
            for version in range(2, SCHEMA_VERSION + 1):
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)",
                           (version, int(time.time()), encode({"postgres_bootstrap": True})))
