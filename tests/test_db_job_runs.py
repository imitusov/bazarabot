"""Tests for zarabot.db.job_runs — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zarabot.db.connection import connect, disconnect
from zarabot.db.job_runs import has_run, last_run, mark_run
from zarabot.db.migrations import apply

NOW = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    conn = await connect(str(path))
    await apply(conn)
    try:
        yield path
    finally:
        await disconnect()


async def test_marked_job_has_run_for_that_period_only(db: Path) -> None:
    assert await has_run("heartbeat", "2026-03-16") is False
    await mark_run("heartbeat", "2026-03-16", NOW)
    assert await has_run("heartbeat", "2026-03-16") is True
    assert await has_run("heartbeat", "2026-03-17") is False
    assert await has_run("rollover", "2026-03-16") is False


async def test_marking_twice_keeps_the_first_moment(db: Path) -> None:
    """When the job FIRST completed is what makes a late run distinguishable
    from a repeated one."""
    await mark_run("weekly_report", "2026-03-15", NOW)
    await mark_run("weekly_report", "2026-03-15", NOW + timedelta(hours=2))
    assert await last_run("weekly_report") == NOW


async def test_last_run_is_none_for_a_job_that_never_ran(db: Path) -> None:
    """ "Did the weekly report go out?" must be answerable, including when the
    answer is no — it was not, while the answer lived in a module global."""
    assert await last_run("weekly_report") is None


async def test_last_run_returns_the_most_recent_period(db: Path) -> None:
    await mark_run("rollover", "2026-03-16", NOW)
    await mark_run("rollover", "2026-03-17", NOW + timedelta(days=1))
    assert await last_run("rollover") == NOW + timedelta(days=1)


async def test_naive_ran_at_is_rejected(db: Path) -> None:
    naive = datetime(2026, 3, 16, 10, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        await mark_run("heartbeat", "2026-03-16", naive)


async def test_module_never_opens_or_commits_its_own_connection() -> None:
    import zarabot.db.job_runs as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    for statement in ("BEGIN", "commit()", "rollback()"):
        assert statement not in source
