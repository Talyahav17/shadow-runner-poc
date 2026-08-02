"""Thin dual-backend layer: SQLite (default, file-based, zero setup) or
PostgreSQL (via DATABASE_URL) for production scale.

SQLite's single-writer lock (even in WAL mode) becomes a real bottleneck
once shadow-comparison volume and concurrent dashboard reads grow past
what a small deployment needs. Both shadow_store.py and migration_store.py
go through this module instead of calling sqlite3 directly, so switching
backends is an environment variable, not a code change.
"""

import sqlite3

import secrets_helper

# DATABASE_URL_FILE (a path to a file containing the connection string) is
# also honored -- see secrets_helper.py.
DATABASE_URL = secrets_helper.read_config("DATABASE_URL")


def is_postgres() -> bool:
    return bool(DATABASE_URL)


def placeholder() -> str:
    """'?' for SQLite, '%s' for PostgreSQL -- build query strings with
    this instead of hardcoding either paramstyle."""
    return "%s" if DATABASE_URL else "?"


def autoincrement_pk() -> str:
    """The CREATE TABLE column-definition fragment for an autoincrementing
    integer primary key, which differs per dialect."""
    return "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"


class Connection:
    """Wraps either a sqlite3 or a psycopg2 connection behind one small,
    uniform interface -- just enough for the simple CRUD both stores do,
    not a general-purpose ORM."""

    def __init__(self, raw):
        self._raw = raw
        self._pg = is_postgres()

    def execute(self, query: str, params=()):
        """Returns the underlying cursor (its .fetchone()/.fetchall() and
        .description both work the same way on sqlite3 and psycopg2)."""
        if self._pg:
            cur = self._raw.cursor()
            cur.execute(query, params)
            return cur
        return self._raw.execute(query, params)

    def executescript(self, script: str) -> None:
        """Runs a multi-statement DDL script. SQLite has a native method
        for this; psycopg2 doesn't, so split on ';' -- fine for our own
        fixed, simple schema strings (no ';' inside string literals)."""
        if self._pg:
            cur = self._raw.cursor()
            for stmt in filter(None, (s.strip() for s in script.split(";"))):
                cur.execute(stmt)
        else:
            self._raw.executescript(script)

    def fetchall_dicts(self, query: str, params=()) -> list:
        cur = self.execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def fetchone_dict(self, query: str, params=()):
        cur = self.execute(query, params)
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row)) if row else None

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def connect(sqlite_path: str) -> Connection:
    """sqlite_path is ignored when DATABASE_URL is set -- Postgres
    connects by URL, not by file path."""
    if DATABASE_URL:
        import psycopg2
        raw = psycopg2.connect(DATABASE_URL)
    else:
        raw = sqlite3.connect(sqlite_path)
    return Connection(raw)
