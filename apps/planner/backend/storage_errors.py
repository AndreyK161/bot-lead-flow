"""Database exception families without requiring psycopg for SQLite-only tools."""
import sqlite3

DATABASE_ERRORS = (sqlite3.Error,)
INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
try:
    import psycopg
except ModuleNotFoundError:
    pass
else:
    DATABASE_ERRORS += (psycopg.Error,)
    INTEGRITY_ERRORS += (psycopg.IntegrityError,)
