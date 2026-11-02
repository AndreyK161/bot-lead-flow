"""Explicit, all-or-nothing transfer of Planner v8 into an empty PG namespace.

This module never changes runtime configuration or the bot's public tables.
A dry-run reads SQLite only. Apply requires the dataset digest from that review.
"""
from contextlib import closing, contextmanager
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from .store import SCHEMA_VERSION, Store

TABLES = (
    'employees', 'employee_aliases', 'plans', 'tasks', 'goal_requests', 'audit',
    'reports', 'distributions', 'distribution_tasks', 'distribution_leads',
    'system_audit', 'schema_migrations', 'import_attempts', 'legacy_imports',
)
IDENTITY_TABLES = ('employees', 'plans', 'tasks', 'goal_requests', 'audit', 'system_audit')
SOURCE_VERSION = 8
MAX_INT = 2**63 - 1


class TransferError(Exception):
    """Messages contain schema identifiers only, never records or credentials."""


@lru_cache(maxsize=1)
def _layout():
    if SCHEMA_VERSION != SOURCE_VERSION:
        raise TransferError('Transfer must be reviewed for the new Planner schema version.')
    # Use the real v8 initializer as the reference, not a second column list.
    with tempfile.TemporaryDirectory(prefix='planner-reference-') as directory:
        reference = Store(Path(directory) / 'reference.sqlite3')
        reference.initialize()
        with reference.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != set(TABLES):
                raise TransferError('Transfer layout must be updated for the current schema.')
            return {table: tuple(tuple(r) for r in db.execute(f'PRAGMA table_info("{table}")'))
                    for table in TABLES}


@contextmanager
def _source(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise TransferError('Source must be a SQLite snapshot file.')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')  # All validation, copying and hashes see one snapshot.
        try:
            yield db
        finally:
            db.rollback()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _columns(table):
    return [field[1] for field in _layout()[table]]


def _primary_key(table):
    return [field[1] for field in sorted(_layout()[table], key=lambda f: f[5]) if field[5]]


def _rows(db, table):
    columns = ','.join(f'"{name}"' for name in _columns(table))
    kinds = {field[1]: field[2] for field in _layout()[table]}
    order = ','.join(f'"{name}" COLLATE BINARY' if kinds[name] == 'TEXT' else f'"{name}"'
                     for name in _primary_key(table))
    cursor = db.execute(f'SELECT {columns} FROM "{table}" ORDER BY {order}')
    while batch := cursor.fetchmany(500):
        yield from batch


def _normalized(table, row):
    values = []
    for field, value in zip(_layout()[table], row, strict=True):
        _, name, kind, required, _, primary = field
        if value is None:
            if required or primary:
                raise TransferError(f'NULL in required column {table}.{name}.')
        elif kind == 'INTEGER':
            if type(value) is not int or not -MAX_INT - 1 <= value <= MAX_INT:
                raise TransferError(f'Unsupported integer in {table}.{name}.')
            if table in IDENTITY_TABLES and name == 'id' and value == MAX_INT:
                raise TransferError(f'No room for the next identity in {table}.')
        elif kind == 'REAL':
            if type(value) not in (int, float) or not math.isfinite(value):
                raise TransferError(f'Unsupported numeric value in {table}.{name}.')
            value = float(value) if value else 0.0
        elif kind == 'TEXT':
            if not isinstance(value, str) or '\x00' in value:
                raise TransferError(f'Unsupported text in {table}.{name}.')
        else:
            raise TransferError(f'Unsupported column type in {table}.{name}.')
        values.append(value)
    return values


def _table_digest(table, rows):
    digest = hashlib.sha256(_json(_columns(table)) + b'\n')
    count = 0
    for row in rows:
        digest.update(_json(_normalized(table, row)) + b'\n')
        count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def _validate_source(db):
    if db.execute('PRAGMA user_version').fetchone()[0] != SOURCE_VERSION:
        raise TransferError('Only the current Planner SQLite v8 snapshot is supported.')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise TransferError('SQLite integrity check failed.')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise TransferError('SQLite foreign key check failed.')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables != set(TABLES):
        raise TransferError('Unexpected SQLite tables; refusing partial transfer.')
    for table in TABLES:
        actual = tuple(tuple(r) for r in db.execute(f'PRAGMA table_info("{table}")'))
        if actual != _layout()[table]:
            raise TransferError(f'Unexpected SQLite columns in {table}.')
    manifest = {table: _table_digest(table, _rows(db, table)) for table in TABLES}
    digest = hashlib.sha256(_json({'version': SCHEMA_VERSION, 'tables': manifest})).hexdigest()
    return {'schema_version': SCHEMA_VERSION, 'source_data_sha256': digest, 'tables': manifest}


def inspect_source(path):
    """No PostgreSQL connection, schema creation or source writes."""
    with _source(path) as db:
        return {'mode': 'dry-run', 'target_checked': False, **_validate_source(db)}


def _require_empty_target(connection):
    # Include sequences, views, functions and custom types, not only table rows.
    for catalog, namespace in (('pg_class', 'relnamespace'), ('pg_proc', 'pronamespace'),
                               ('pg_type', 'typnamespace')):
        if connection.execute(
            f'SELECT 1 FROM pg_catalog.{catalog} o JOIN pg_catalog.pg_namespace n '
            f"ON n.oid=o.{namespace} WHERE n.nspname='planner' LIMIT 1"
        ).fetchone() is not None:
            raise TransferError('The planner schema is not empty; no data was changed.')


def inspect_target(database_url):
    from .postgres import PostgresStore
    with PostgresStore(database_url).connection() as db:
        _require_empty_target(db.connection)
    return {'target_checked': True, 'target_empty': True}


def _copy_table(source, target, table):
    from psycopg import sql
    columns = _columns(table)
    statement = sql.SQL('COPY {} ({}) FROM STDIN').format(
        sql.Identifier('planner', table), sql.SQL(',').join(map(sql.Identifier, columns)))
    with target.cursor() as cursor, cursor.copy(statement) as copy:
        for row in _rows(source, table):
            values = list(row)
            # Both links can point to IDs later in the stream (or backwards).
            # Restore them after all tasks exist, keeping FK checks enabled.
            if table == 'tasks':
                for name in ('moved_to', 'source_task_id'):
                    values[columns.index(name)] = None
            copy.write_row(values)


def _restore_task_links(source, target):
    rows = source.execute('SELECT moved_to,source_task_id,id FROM tasks '
                          'WHERE moved_to IS NOT NULL OR source_task_id IS NOT NULL')
    with target.cursor() as cursor:
        while batch := rows.fetchmany(500):
            cursor.executemany('UPDATE planner.tasks SET moved_to=%s,source_task_id=%s WHERE id=%s', batch)


def _verify(source_manifest, target):
    from psycopg import sql
    verified = {}
    for table in TABLES:
        statement = sql.SQL('SELECT {} FROM {} ORDER BY {}').format(
            sql.SQL(',').join(map(sql.Identifier, _columns(table))),
            sql.Identifier('planner', table),
            sql.SQL(',').join(
                sql.SQL('{} COLLATE "C"').format(sql.Identifier(field[1]))
                if field[2] == 'TEXT' else sql.Identifier(field[1])
                for field in sorted(_layout()[table], key=lambda f: f[5]) if field[5]))
        with target.cursor(name='verify_' + uuid4().hex) as cursor:
            cursor.execute(statement)
            actual = _table_digest(table, ([row[name] for name in _columns(table)] for row in cursor))
        if actual != source_manifest[table]:
            raise TransferError(f'Content verification failed for {table}.')
        verified[table] = actual
    return verified


def _restart_identities(source, target):
    from psycopg import sql
    for table in IDENTITY_TABLES:
        maximum = source.execute(f'SELECT max(id) FROM "{table}"').fetchone()[0]
        next_id = max(1, (maximum or 0) + 1)
        # RESTART is transactional. Never advance any existing/public sequence.
        target.execute(sql.SQL('ALTER TABLE {} ALTER COLUMN id RESTART WITH {}').format(
            sql.Identifier('planner', table), sql.Literal(next_id)))


def transfer(path, database_url, *, expected_source_sha256):
    """Commit schema, rows, links and sequences only after every hash matches."""
    from .postgres import PostgresStore
    if not database_url:
        raise TransferError('An explicit Planner migration database URL is required.')
    if not isinstance(expected_source_sha256, str) or len(expected_source_sha256) != 64:
        raise TransferError('Apply requires the source_data_sha256 from a dry-run.')
    with _source(path) as source:
        reviewed = _validate_source(source)
        if reviewed['source_data_sha256'] != expected_source_sha256:
            raise TransferError('Source changed since dry-run; review a new snapshot first.')
        with PostgresStore(database_url).connection(True) as db:
            target = db.connection
            _require_empty_target(target)
            target.execute('CREATE SCHEMA IF NOT EXISTS planner')
            target.execute(Path(__file__).with_name('postgres_schema.sql').read_text())
            for table in TABLES:
                _copy_table(source, target, table)
            _restore_task_links(source, target)
            verified = _verify(reviewed['tables'], target)
            _restart_identities(source, target)
        # Leaving the context above has committed; never report success earlier.
        return {'mode': 'applied', 'target_checked': True, 'verified': True,
                **reviewed, 'tables': verified}


def verify_transfer(path, database_url):
    """Read-only comparison after import or restore; no sequence consumption."""
    from .postgres import PostgresStore
    store = PostgresStore(database_url)
    with _source(path) as source:
        reviewed = _validate_source(source)
        with store.connection() as db:
            if store.schema_version(db) != SOURCE_VERSION:
                raise TransferError('Unexpected destination schema version.')
            verified = _verify(reviewed['tables'], db.connection)
        return {'mode': 'verified', 'target_checked': True, 'verified': True,
                **reviewed, 'tables': verified}
