"""Shared test fixtures.

The database fixture lives here rather than in each test file: `db.connection`
owns one connection per process, so a fixture that forgets to disconnect leaks
it into whichever test runs next. Defining it once is the point (spec §4,
`zarabot/db/connection.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from zarabot.db.connection import connect, disconnect
from zarabot.db.migrations import apply


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Path]:
    """Connect a temporary database, apply migrations, disconnect on teardown."""
    path = tmp_path / "zarabot.db"
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()
