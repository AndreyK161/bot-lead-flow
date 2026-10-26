"""Persistence layer: PostgreSQL in production, SQLite fallback for local tests."""
from __future__ import annotations

import sqlite3
import json
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
        CREATE TABLE IF NOT EXISTS user_ui_modes (
            telegram_id BIGINT PRIMARY KEY,
            mode TEXT NOT NULL CHECK(mode IN ('supervisor','manager')),
            updated_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS distribution_sessions (
            telegram_id BIGINT PRIMARY KEY,
            session_id TEXT NOT NULL,
            step TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS distribution_reminders (
            chat_id BIGINT NOT NULL,
            thread_id BIGINT NOT NULL DEFAULT 0,
            enabled_by BIGINT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_attempt_date TEXT,
            PRIMARY KEY (chat_id,thread_id)
        );
        CREATE TABLE IF NOT EXISTS planner_access (
            telegram_id BIGINT PRIMARY KEY,
            bitrix_user_id TEXT,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','manager','supervisor')),
            active INTEGER NOT NULL DEFAULT 1,
            login_enabled INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            legacy_created_at TEXT,
            updated_at {timestamp} DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_planner_access_bitrix
            ON planner_access(bitrix_user_id) WHERE bitrix_user_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS manager_links (
            bitrix_user_id TEXT NOT NULL, bitrix_name TEXT, telegram_id BIGINT NOT NULL,
            linked_at {timestamp} DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bitrix_user_id, telegram_id), UNIQUE (telegram_id)
        );
        CREATE INDEX IF NOT EXISTS idx_manager_links_manager ON manager_links(bitrix_user_id);
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
        CREATE TABLE IF NOT EXISTS megafon_calls (
            call_id TEXT PRIMARY KEY,
            manager_extension TEXT, direction TEXT, client_phone TEXT,
            started_at {timestamp} NOT NULL, duration_seconds INTEGER NOT NULL DEFAULT 0,
            wait_seconds INTEGER NOT NULL DEFAULT 0,
            call_status TEXT, recording_url TEXT,
            processing_status TEXT NOT NULL DEFAULT 'pending', transcript TEXT,
            outcome_kind TEXT, next_call_at TEXT, rejection_reason TEXT,
            outcome_summary TEXT, last_error TEXT,
            created_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp} NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_megafon_calls_phone_started
            ON megafon_calls(client_phone,started_at);
    '''


class _Connection:
    def __init__(self, connection, *, postgres: bool):
        self._connection = connection
        self._postgres = postgres

    def execute(self, sql, params=()):
        return self._connection.execute(sql.replace("?", "%s") if self._postgres else sql, params)


def _initialize_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema(postgres=False))
    # One Bitrix employee may use more than one explicitly authorized Telegram account.
    conn.execute("DROP INDEX IF EXISTS idx_planner_access_bitrix")
    conn.execute(
        "CREATE INDEX idx_planner_access_bitrix ON planner_access(bitrix_user_id) "
        "WHERE bitrix_user_id IS NOT NULL"
    )
    link_columns = list(conn.execute("PRAGMA table_info(manager_links)"))
    link_primary_key = [row[1] for row in sorted(link_columns, key=lambda row: row[5]) if row[5]]
    if link_primary_key == ["bitrix_user_id"]:
        conn.executescript('''
            ALTER TABLE manager_links RENAME TO manager_links_legacy;
            CREATE TABLE manager_links (
                bitrix_user_id TEXT NOT NULL, bitrix_name TEXT, telegram_id BIGINT NOT NULL,
                linked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bitrix_user_id, telegram_id), UNIQUE (telegram_id)
            );
            INSERT INTO manager_links (bitrix_user_id,bitrix_name,telegram_id,linked_at)
                SELECT bitrix_user_id,bitrix_name,telegram_id,linked_at FROM manager_links_legacy;
            DROP TABLE manager_links_legacy;
            CREATE INDEX IF NOT EXISTS idx_manager_links_manager ON manager_links(bitrix_user_id);
        ''')
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
    call_columns = {row[1] for row in conn.execute("PRAGMA table_info(megafon_calls)")}
    if "wait_seconds" not in call_columns:
        conn.execute("ALTER TABLE megafon_calls ADD COLUMN wait_seconds INTEGER NOT NULL DEFAULT 0")


def _initialize_postgres(conn) -> None:
    for statement in _schema(postgres=True).split(";"):
        if statement.strip():
            conn.execute(statement)
    for statement in (
        "DROP INDEX IF EXISTS idx_planner_access_bitrix",
        "CREATE INDEX idx_planner_access_bitrix ON planner_access(bitrix_user_id) WHERE bitrix_user_id IS NOT NULL",
        "ALTER TABLE manager_links DROP CONSTRAINT IF EXISTS manager_links_pkey",
        "ALTER TABLE manager_links ADD CONSTRAINT manager_links_pkey PRIMARY KEY (bitrix_user_id,telegram_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_links_telegram_id ON manager_links(telegram_id)",
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
        "ALTER TABLE megafon_calls ADD COLUMN IF NOT EXISTS wait_seconds INTEGER NOT NULL DEFAULT 0",
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


def user_ui_mode(telegram_id: int) -> str:
    result = rows("SELECT mode FROM user_ui_modes WHERE telegram_id=?", (telegram_id,))
    return result[0]["mode"] if result else "supervisor"


def set_user_ui_mode(telegram_id: int, mode: str) -> None:
    if mode not in {"supervisor", "manager"}:
        raise ValueError("Unknown UI mode")
    execute(
        "INSERT INTO user_ui_modes (telegram_id,mode,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(telegram_id) DO UPDATE SET mode=excluded.mode,updated_at=CURRENT_TIMESTAMP",
        (telegram_id, mode),
    )


def distribution_session(telegram_id: int) -> dict | None:
    result = rows("SELECT * FROM distribution_sessions WHERE telegram_id=?", (telegram_id,))
    if not result:
        return None
    session = result[0]
    try:
        payload = json.loads(session.pop("payload_json"))
    except (TypeError, ValueError):
        return None
    return {**session, "payload": payload}


def save_distribution_session(telegram_id: int, session_id: str, step: str, payload: dict) -> None:
    execute(
        "INSERT INTO distribution_sessions (telegram_id,session_id,step,payload_json,updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(telegram_id) DO UPDATE SET "
        "session_id=excluded.session_id,step=excluded.step,payload_json=excluded.payload_json,"
        "updated_at=CURRENT_TIMESTAMP",
        (telegram_id, session_id, step, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    )


def clear_distribution_session(telegram_id: int) -> None:
    execute("DELETE FROM distribution_sessions WHERE telegram_id=?", (telegram_id,))


def set_distribution_reminder(chat_id: int, enabled_by: int, enabled: bool, thread_id: int = 0) -> None:
    execute(
        "INSERT INTO distribution_reminders (chat_id,thread_id,enabled_by,enabled) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id,thread_id) DO UPDATE SET enabled_by=excluded.enabled_by,enabled=excluded.enabled",
        (chat_id, thread_id, enabled_by, int(enabled)),
    )


def distribution_reminders() -> list[dict]:
    return rows(
        "SELECT chat_id,thread_id,enabled_by,last_attempt_date FROM distribution_reminders "
        "WHERE enabled=1 ORDER BY chat_id,thread_id"
    )


def claim_distribution_reminder(chat_id: int, thread_id: int, day: str) -> bool:
    with db() as conn:
        cursor = conn.execute(
            "UPDATE distribution_reminders SET last_attempt_date=? "
            "WHERE chat_id=? AND thread_id=? AND enabled=1 "
            "AND (last_attempt_date IS NULL OR last_attempt_date<?)",
            (day, chat_id, thread_id, day),
        )
        return cursor.rowcount == 1


def upsert_planner_access(
    telegram_id: int,
    *,
    bitrix_user_id: str | None,
    display_name: str,
    role: str,
    active: bool,
    source: str,
    legacy_created_at: str | None = None,
    login_enabled_on_insert: bool = False,
) -> None:
    if role not in {"owner", "manager", "supervisor"}:
        raise ValueError("Unknown planner role")
    execute(
        "INSERT INTO planner_access "
        "(telegram_id,bitrix_user_id,display_name,role,active,login_enabled,source,legacy_created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(telegram_id) DO UPDATE SET "
        "bitrix_user_id=excluded.bitrix_user_id,display_name=excluded.display_name,"
        "role=excluded.role,active=excluded.active,source=excluded.source,"
        "legacy_created_at=excluded.legacy_created_at,updated_at=CURRENT_TIMESTAMP",
        (
            telegram_id, bitrix_user_id, display_name, role, int(active),
            int(login_enabled_on_insert), source, legacy_created_at,
        ),
    )


def planner_access(telegram_id: int) -> dict | None:
    result = rows("SELECT * FROM planner_access WHERE telegram_id=?", (telegram_id,))
    return result[0] if result else None


def list_planner_access() -> list[dict]:
    return rows(
        "SELECT * FROM planner_access "
        "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'supervisor' THEN 1 ELSE 2 END,display_name,telegram_id"
    )


def set_planner_login_enabled(telegram_id: int, enabled: bool) -> bool:
    with db() as conn:
        cursor = conn.execute(
            "UPDATE planner_access SET login_enabled=?,updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?",
            (int(enabled), telegram_id),
        )
        return cursor.rowcount == 1


def link_manager(bitrix_user_id, bitrix_name, telegram_id):
    execute(
        "INSERT INTO manager_links (bitrix_user_id,bitrix_name,telegram_id) VALUES (?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET bitrix_user_id=excluded.bitrix_user_id,"
        "bitrix_name=excluded.bitrix_name,linked_at=CURRENT_TIMESTAMP",
        (str(bitrix_user_id), bitrix_name, telegram_id),
    )


def manager_telegram_id(bitrix_user_id):
    result = manager_telegram_ids(bitrix_user_id)
    return result[0] if result else None


def manager_telegram_ids(bitrix_user_id):
    return [
        row["telegram_id"]
        for row in rows(
            "SELECT telegram_id FROM manager_links WHERE bitrix_user_id=? ORDER BY linked_at,telegram_id",
            (str(bitrix_user_id),),
        )
    ]


def list_links():
    return rows("SELECT * FROM manager_links ORDER BY bitrix_name,telegram_id")


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
