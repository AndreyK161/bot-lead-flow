"""Persistence layer: PostgreSQL in production, SQLite fallback for local tests."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from app.config import get_settings

_initialized: set[str] = set()
_initialize_lock = Lock()


def _schema(*, postgres: bool) -> str:
    timestamp = "TIMESTAMPTZ" if postgres else "TEXT"
    event_id = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return f'''
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY, update_id BIGINT UNIQUE, chat_id BIGINT,
            message_id BIGINT, contact TEXT, state TEXT DEFAULT 'draft',
            source_id TEXT, source_name TEXT, lead_id TEXT UNIQUE,
            manager TEXT, manager_id TEXT, comment TEXT, bot_kind TEXT DEFAULT 'legacy',
            main_message_id BIGINT, dirty INTEGER DEFAULT 1, duplicate_of TEXT,
            created_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_updates (update_id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS deliveries (
            submission_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id BIGINT NOT NULL,
            PRIMARY KEY (submission_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS telegram_starts (
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            started_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS manager_links (
            bitrix_user_id TEXT PRIMARY KEY, bitrix_name TEXT, telegram_id BIGINT NOT NULL,
            linked_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deal_notifications (
            deal_id TEXT PRIMARY KEY, dirty INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS deal_deliveries (
            deal_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id BIGINT NOT NULL,
            PRIMARY KEY (deal_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen_leads (
            lead_id TEXT PRIMARY KEY, seen_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS seen_lead_phones (
            lead_id TEXT NOT NULL, phone TEXT NOT NULL,
            PRIMARY KEY (lead_id, phone)
        );
        CREATE TABLE IF NOT EXISTS crm_items (
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            source_id TEXT, source_name TEXT, created_date TEXT NOT NULL,
            current_stage_id TEXT, current_stage_name TEXT, current_category_id TEXT,
            current_assignee_id TEXT, current_assignee_name TEXT,
            processed_at TEXT, processed_by_id TEXT, processed_by_name TEXT,
            origin_lead_id TEXT, linked_deal_id TEXT,
            outcome_entity_type TEXT, outcome_entity_id TEXT, outcome_category_id TEXT,
            outcome_stage_id TEXT, outcome_stage_name TEXT,
            contract_deal_id TEXT, contract_at TEXT, outcome_checked_at TEXT,
            first_seen_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_id)
        );
        CREATE TABLE IF NOT EXISTS crm_item_events (
            id {event_id}, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL, old_value TEXT, new_value TEXT,
            occurred_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bitrix_events (
            id {event_id}, payload_hash TEXT NOT NULL UNIQUE,
            event_name TEXT NOT NULL, entity_type TEXT, entity_id TEXT,
            event_at {timestamp}, received_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            processed_at {timestamp}
        );
        CREATE INDEX IF NOT EXISTS idx_bitrix_events_status_received
            ON bitrix_events(status,received_at);
        CREATE INDEX IF NOT EXISTS idx_crm_item_events_entity_time
            ON crm_item_events(entity_type,entity_id,occurred_at);
        CREATE TABLE IF NOT EXISTS daily_report_deliveries (
            report_date TEXT NOT NULL, chat_id TEXT NOT NULL, message_id BIGINT,
            PRIMARY KEY (report_date, chat_id)
        );
    '''


class _Connection:
    def __init__(self, connection, *, postgres: bool):
        self._connection = connection
        self._postgres = postgres

    def execute(self, sql, params=()):
        return self._connection.execute(sql.replace("?", "%s") if self._postgres else sql, params)


def _initialize_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema(postgres=False))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
    additions = {
        "manager_id": "TEXT",
        "comment": "TEXT",
        "bot_kind": "TEXT DEFAULT 'legacy'",
        "duplicate_of": "TEXT",
        "created_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE submissions ADD COLUMN {name} {definition}")
    conn.execute("UPDATE submissions SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
    crm_columns = {row[1] for row in conn.execute("PRAGMA table_info(crm_items)")}
    for name in (
        "current_stage_name", "current_category_id", "origin_lead_id", "linked_deal_id",
        "outcome_entity_type", "outcome_entity_id", "outcome_category_id",
        "outcome_stage_id", "outcome_stage_name", "contract_deal_id", "contract_at",
        "outcome_checked_at",
    ):
        if name not in crm_columns:
            conn.execute(f"ALTER TABLE crm_items ADD COLUMN {name} TEXT")


def _initialize_postgres(conn) -> None:
    for statement in _schema(postgres=True).split(";"):
        if statement.strip():
            conn.execute(statement)
    for statement in (
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS manager_id TEXT",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS comment TEXT",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS bot_kind TEXT DEFAULT 'legacy'",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS duplicate_of TEXT",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS current_stage_name TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS current_category_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS origin_lead_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS linked_deal_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_entity_type TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_entity_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_category_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_stage_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_stage_name TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS contract_deal_id TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS contract_at TEXT",
        "ALTER TABLE crm_items ADD COLUMN IF NOT EXISTS outcome_checked_at TEXT",
    ):
        conn.execute(statement)


@contextmanager
def db():
    settings = get_settings()
    database_url = str(getattr(settings, "database_url", "") or "")
    postgres = bool(database_url)
    if postgres:
        import psycopg
        from psycopg.rows import dict_row

        target = database_url
        conn = psycopg.connect(database_url, row_factory=dict_row)
    else:
        path = Path(settings.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = str(path.resolve())
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")

    try:
        if target not in _initialized:
            with _initialize_lock:
                if target not in _initialized:
                    _initialize_postgres(conn) if postgres else _initialize_sqlite(conn)
                    conn.commit()
                    _initialized.add(target)
        adapter = _Connection(conn, postgres=postgres)
        try:
            yield adapter
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def execute(sql, params=()):
    with db() as conn:
        conn.execute(sql, params)


def rows(sql, params=()):
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


@contextmanager
def transaction():
    """Expose one portable transaction for multi-statement state changes."""
    with db() as conn:
        yield conn


def submission(identifier):
    result = rows("SELECT * FROM submissions WHERE id=?", (identifier,))
    return result[0] if result else None


def by_lead(lead_id):
    result = rows("SELECT * FROM submissions WHERE lead_id=?", (str(lead_id),))
    return result[0] if result else None


def save(identifier, **fields):
    execute("UPDATE submissions SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), identifier))


def record_start(telegram_id, username, first_name):
    execute(
        "INSERT INTO telegram_starts (telegram_id,username,first_name) VALUES (?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",
        (telegram_id, username, first_name),
    )


def recent_starts(limit=20):
    return rows("SELECT * FROM telegram_starts ORDER BY started_at DESC LIMIT ?", (limit,))


def link_manager(bitrix_user_id, bitrix_name, telegram_id):
    execute(
        "INSERT INTO manager_links (bitrix_user_id,bitrix_name,telegram_id) VALUES (?,?,?) "
        "ON CONFLICT(bitrix_user_id) DO UPDATE SET bitrix_name=excluded.bitrix_name,telegram_id=excluded.telegram_id",
        (str(bitrix_user_id), bitrix_name, telegram_id),
    )


def manager_telegram_id(bitrix_user_id):
    result = rows("SELECT telegram_id FROM manager_links WHERE bitrix_user_id=?", (str(bitrix_user_id),))
    return result[0]["telegram_id"] if result else None


def list_links():
    return rows("SELECT * FROM manager_links ORDER BY bitrix_name")


def _normalized_phone(value):
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def record_seen_lead(lead):
    lead_id = str(lead.get("ID") or "")
    if not lead_id:
        return
    execute("INSERT INTO seen_leads (lead_id) VALUES (?) ON CONFLICT DO NOTHING", (lead_id,))
    for item in lead.get("PHONE") or []:
        phone = _normalized_phone((item or {}).get("VALUE"))
        if phone:
            execute("INSERT INTO seen_lead_phones VALUES (?,?) ON CONFLICT DO NOTHING", (lead_id, phone))


def was_lead_seen(lead_id):
    if not lead_id:
        return False
    return bool(rows("SELECT 1 FROM seen_leads WHERE lead_id=?", (str(lead_id),))) or by_lead(lead_id) is not None


def find_all_seen_lead_ids_by_phones(raw_phones):
    """Все ID лидов (из локального кэша), чей нормализованный телефон совпадает — для антидубль-проверки."""
    phones = [_normalized_phone(value) for value in raw_phones]
    phones = [phone for phone in phones if phone]
    if not phones:
        return []
    placeholders = ','.join('?' for _ in phones)
    result = rows(f'SELECT DISTINCT lead_id FROM seen_lead_phones WHERE phone IN ({placeholders})', tuple(phones))
    return [row['lead_id'] for row in result]


def find_seen_lead_by_phones(raw_phones):
    phones = [_normalized_phone(value) for value in raw_phones]
    phones = [phone for phone in phones if phone]
    if not phones:
        return None
    placeholders = ",".join("?" for _ in phones)
    result = rows(
        f"""SELECT phones.lead_id FROM seen_lead_phones AS phones
            JOIN seen_leads AS leads ON leads.lead_id=phones.lead_id
            WHERE phones.phone IN ({placeholders}) ORDER BY leads.seen_at DESC LIMIT 1""",
        tuple(phones),
    )
    return result[0]["lead_id"] if result else None
