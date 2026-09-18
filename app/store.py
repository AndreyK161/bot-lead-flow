"""Persistent manual submissions and the bot's enabled Bitrix sources."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings


@contextmanager
def db():
    path = Path(get_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY, update_id INTEGER UNIQUE, chat_id INTEGER,
            message_id INTEGER, contact TEXT, state TEXT DEFAULT 'draft',
            source_id TEXT, source_name TEXT, lead_id TEXT UNIQUE,
            manager TEXT, main_message_id INTEGER, dirty INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_updates (update_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS deliveries (
            submission_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY (submission_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS telegram_starts (
            telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS manager_links (
            bitrix_user_id TEXT PRIMARY KEY, bitrix_name TEXT, telegram_id INTEGER NOT NULL,
            linked_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deal_notifications (
            deal_id TEXT PRIMARY KEY, dirty INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS deal_deliveries (
            deal_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY (deal_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    ''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def execute(sql, params=()):
    with db() as conn:
        conn.execute(sql, params)


def rows(sql, params=()):
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def submission(identifier):
    result = rows('SELECT * FROM submissions WHERE id=?', (identifier,))
    return result[0] if result else None


def by_lead(lead_id):
    result = rows('SELECT * FROM submissions WHERE lead_id=?', (str(lead_id),))
    return result[0] if result else None


def save(identifier, **fields):
    execute('UPDATE submissions SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', (*fields.values(), identifier))


def record_start(telegram_id, username, first_name):
    execute(
        'INSERT INTO telegram_starts (telegram_id, username, first_name) VALUES (?,?,?) '
        'ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',
        (telegram_id, username, first_name),
    )


def recent_starts(limit=20):
    return rows('SELECT * FROM telegram_starts ORDER BY started_at DESC LIMIT ?', (limit,))


def link_manager(bitrix_user_id, bitrix_name, telegram_id):
    execute(
        'INSERT INTO manager_links (bitrix_user_id, bitrix_name, telegram_id) VALUES (?,?,?) '
        'ON CONFLICT(bitrix_user_id) DO UPDATE SET bitrix_name=excluded.bitrix_name, telegram_id=excluded.telegram_id',
        (str(bitrix_user_id), bitrix_name, telegram_id),
    )


def manager_telegram_id(bitrix_user_id):
    result = rows('SELECT telegram_id FROM manager_links WHERE bitrix_user_id=?', (str(bitrix_user_id),))
    return result[0]['telegram_id'] if result else None


def list_links():
    return rows('SELECT * FROM manager_links ORDER BY bitrix_name')
