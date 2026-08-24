"""Sole owner of the process-wide SQLite connection.

No other module calls ``aiosqlite.connect`` or closes this connection.
Must never connect at import.
"""

from __future__ import annotations

import aiosqlite

_connection: aiosqlite.Connection | None = None


class DatabaseNotOpenError(Exception):
    """Raised when the process connection is missing (rule 30)."""


class DatabaseAlreadyOpenError(Exception):
    """Raised when connect is called while a process connection is already open."""


async def connect(path: str) -> aiosqlite.Connection:
    """Open ``path``, store it as the process connection, and set WAL/FK/timeout."""
    global _connection
    if _connection is not None:
        raise DatabaseAlreadyOpenError("process database connection is already open")
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 30000")
    _connection = conn
    return conn


def shared() -> aiosqlite.Connection:
    """Return the open process connection. Never opens a fallback."""
    if _connection is None:
        raise DatabaseNotOpenError(
            "database is not open; call db.connection.connect first"
        )
    return _connection


async def disconnect() -> None:
    """Close the process connection and forget it. Idempotent when already closed."""
    global _connection
    if _connection is None:
        return
    await _connection.close()
    _connection = None
