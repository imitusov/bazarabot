"""Sole owner of the process-wide SQLite connection.

No other module calls ``aiosqlite.connect`` or closes this connection.
Must never connect at import.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

_LOG = logging.getLogger(__name__)
_TABLE_IN_ERROR = (
    re.compile(r"no such table: (\S+)"),
    re.compile(r"constraint failed: (\w+)"),
    re.compile(r"table (\S+) already exists"),
)

_connection: aiosqlite.Connection | None = None
# The transaction lock belongs to the connection, not to the module: a lock
# created at import binds to whichever event loop first acquires it, and every
# later loop then fails with "bound to a different event loop".
_txn_lock: asyncio.Lock | None = None
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("_depth", default=0)


class DatabaseNotOpenError(Exception):
    """Raised when the process connection is missing (rule 30)."""


class DatabaseAlreadyOpenError(Exception):
    """Raised when connect is called while a process connection is already open."""


def _table_from_error(exc: BaseException) -> str:
    text = str(exc)
    for pattern in _TABLE_IN_ERROR:
        match = pattern.search(text)
        if match:
            return match.group(1).rstrip(".").split(".")[0]
    return "unknown"


async def connect(path: str) -> aiosqlite.Connection:
    """Open ``path``, store it as the process connection, and set WAL/FK/timeout."""
    global _connection
    if _connection is not None:
        raise DatabaseAlreadyOpenError("process database connection is already open")
    conn = await aiosqlite.connect(path)
    global _txn_lock
    _txn_lock = asyncio.Lock()
    # Set once, here: the row factory is a property of the connection, and seven
    # modules assigning it on a connection they share is a race waiting to happen.
    conn.row_factory = aiosqlite.Row
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
    global _txn_lock
    if _connection is None:
        return
    await _connection.close()
    _connection = None
    _txn_lock = None


@asynccontextmanager
async def transaction(*, critical: bool = True) -> AsyncIterator[aiosqlite.Connection]:
    """The sole transaction owner. Every write runs inside this (rule 31).

    Reentrant: a nested acquisition on the same task joins the outer
    transaction, because `broker.reconcile` calls `db.positions` writers while
    doing work of its own. Only the outermost exit commits. Nested calls ignore
    their own ``critical`` flag; the outermost argument is the one logged.
    """
    if _depth.get() > 0:
        yield shared()
        return
    lock = _txn_lock
    if lock is None:
        raise DatabaseNotOpenError(
            "database is not open; call db.connection.connect first"
        )
    async with lock:
        conn = shared()
        await conn.execute("BEGIN IMMEDIATE")
        token = _depth.set(1)
        try:
            yield conn
        except BaseException as exc:
            await conn.rollback()
            if isinstance(exc, aiosqlite.Error):
                _LOG.error(
                    "database write failed",
                    extra={
                        "event": "db_write_failed",
                        "table": _table_from_error(exc),
                        "critical": critical,
                    },
                )
            raise
        else:
            await conn.commit()
        finally:
            _depth.reset(token)
