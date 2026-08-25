"""Tests for zarabot.db.snapshots — written from technical-spec.md §3.2."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.connection import DatabaseNotOpenError, connect, disconnect
from zarabot.db.migrations import apply
from zarabot.db.snapshots import DailySnapshot, list_for_period, write_daily

DAY = date(2026, 3, 16)

REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _snap(**overrides: object) -> DailySnapshot:
    fields: dict[str, object] = {
        "trade_date": DAY,
        "opening_equity": Decimal("100000.00"),
        "closing_equity": None,
        "cash": Decimal("90000.00"),
        "realised_pnl": Decimal("0"),
        "unrealised_pnl": Decimal("1000.00"),
        "open_positions": 1,
        "orders_placed": 2,
        "benchmark_value": None,
    }
    fields.update(overrides)
    return DailySnapshot(**fields)  # type: ignore[arg-type]


async def test_second_write_for_same_date_updates_not_duplicates(db: Path) -> None:
    await write_daily(_snap())
    await write_daily(
        _snap(
            closing_equity=Decimal("101000.00"),
            cash=Decimal("91000.00"),
            realised_pnl=Decimal("500.00"),
            unrealised_pnl=Decimal("500.00"),
            open_positions=0,
            orders_placed=3,
            benchmark_value=Decimal("1.02"),
        )
    )
    rows = await list_for_period(DAY, DAY)
    assert len(rows) == 1
    snap = rows[0]
    assert snap.trade_date == DAY
    assert snap.opening_equity == Decimal("100000.00")
    assert snap.closing_equity == Decimal("101000.00")
    assert snap.cash == Decimal("91000.00")
    assert snap.realised_pnl == Decimal("500.00")
    assert snap.unrealised_pnl == Decimal("500.00")
    assert snap.open_positions == 0
    assert snap.orders_placed == 3
    assert snap.benchmark_value == Decimal("1.02")
    assert isinstance(snap.opening_equity, Decimal)
    empty = await list_for_period(date(2026, 3, 17), date(2026, 3, 18))
    assert empty == []


async def test_access_without_connect_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "zarabot.db"
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(path))
    with pytest.raises(DatabaseNotOpenError):
        await write_daily(_snap())
    with pytest.raises(DatabaseNotOpenError):
        await list_for_period(DAY, DAY)


async def test_module_never_calls_aiosqlite_connect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zarabot.db.snapshots as module

    source = inspect.getsource(module)
    assert "aiosqlite.connect" not in source
    assert "_connect" not in source
    calls: list[object] = []
    real_connect = aiosqlite.connect

    async def tracking_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        calls.append((args, kwargs))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    await write_daily(_snap())
    rows = await list_for_period(DAY, DAY)
    assert len(rows) == 1
    assert calls == []
