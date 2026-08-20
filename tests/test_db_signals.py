"""Tests for zarabot.db.signals — written from technical-spec.md §3.2."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import pytest

from zarabot.db.migrations import apply
from zarabot.db.signals import list_for_period, record
from zarabot.models import RejectionReason, RiskDecision, Side, Signal

GENERATED = datetime(2026, 3, 16, 10, 0, tzinfo=UTC)

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
    async with aiosqlite.connect(path) as conn:
        await apply(conn)
    return path


def _signal() -> Signal:
    return Signal(
        ticker="SBER",
        strategy="ma_crossover",
        side=Side.BUY,
        generated_at=GENERATED,
        reference_price=Decimal("250.50"),
    )


async def test_rejected_signal_is_stored_with_reason_and_retrievable_by_day(
    db: Path,
) -> None:
    decision = RiskDecision(
        approved=False, lots=None, reason=RejectionReason.COOLDOWN_ACTIVE
    )
    await record(_signal(), decision)
    rows = await list_for_period(date(2026, 3, 16), date(2026, 3, 16))
    assert len(rows) == 1
    signal, stored = rows[0]
    assert signal.ticker == "SBER"
    assert signal.strategy == "ma_crossover"
    assert stored.approved is False
    assert stored.reason is RejectionReason.COOLDOWN_ACTIVE
    assert stored.lots is None
    outside = await list_for_period(date(2026, 3, 17), date(2026, 3, 18))
    assert outside == []
