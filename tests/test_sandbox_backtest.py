"""Tests for sandbox.backtest — written from technical-spec.md §3.2.

The point of the rebuild is that the backtest IS the live path, so these assert
that live machinery is reached — the gate, the cooldown, the position cap —
rather than that some re-derived subset behaves plausibly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sandbox.backtest import run
from sandbox.exchange import Commission
from zarabot.config import Config
from zarabot.models import (
    BacktestResult,
    Candle,
    ExitTrigger,
    Instrument,
    Side,
    Signal,
)

DAY0 = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
COMMISSION = Commission(pct=Decimal("0.05"), minimum=Decimal("1"))


def _config(**overrides: object) -> Config:
    fields: dict[str, object] = {
        "tinvest_token": "t",
        "tinvest_account_id": "a",
        "trading_mode": "live",
        "telegram_bot_token": "tg",
        "telegram_chat_id": 1,
        "allocated_capital": Decimal("100000"),
        "position_size_pct": Decimal("10"),
        "stop_loss_pct": Decimal("5"),
        "take_profit_pct": Decimal("10"),
        "max_holding_days": 3,
        "max_open_positions": 10,
        "reentry_cooldown_minutes": 120,
        "daily_loss_limit_pct": Decimal("5"),
        "watchlist": ("SBER",),
        "enabled_strategies": ("ma_crossover",),
        "ml_model_path": None,
        "poll_interval_seconds": 60,
        "db_path": Path("zarabot.db"),
        "backup_dir": Path("backups"),
        "log_level": "INFO",
        "tz": "Europe/Moscow",
    }
    fields.update(overrides)
    return Config(**fields)  # type: ignore[arg-type]


def _bars(ticker: str, closes: list[str]) -> list[Candle]:
    out = []
    for n, close in enumerate(closes):
        price = Decimal(close)
        out.append(
            Candle(
                timestamp=DAY0 + timedelta(days=n),
                open=price,
                high=price * Decimal("1.001"),
                low=price * Decimal("0.999"),
                close=price,
                volume=1000,
            )
        )
    return out


def _instrument(ticker: str) -> Instrument:
    return Instrument(
        figi=f"BBG-{ticker}",
        ticker=ticker,
        lot=1,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        trading_status="NORMAL_TRADING",
        refreshed_at=DAY0,
    )


class _AlwaysBuy:
    name = "ma_crossover"
    lookback = 1

    def evaluate(self, ticker: str, candles: object, now: datetime) -> Signal:
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=Decimal("100"),
        )


class _NeverBuy:
    name = "ma_crossover"
    lookback = 1

    def evaluate(self, ticker: str, candles: object, now: datetime) -> None:
        return None


async def _run(bars: dict[str, list[Candle]], strategies: tuple[object, ...], **kw):
    return await run(
        bars=bars,
        instruments={t: _instrument(t) for t in bars},
        config=kw.pop("config", _config(watchlist=tuple(bars))),
        strategies=strategies,  # type: ignore[arg-type]
        commission=COMMISSION,
        slippage=Decimal("0"),
        reject_stops=kw.pop("reject_stops", False),
    )


async def test_a_run_returns_a_backtest_result() -> None:
    result = await _run({"SBER": _bars("SBER", ["100"] * 6)}, (_NeverBuy(),))
    assert isinstance(result, BacktestResult)
    assert result.trades == ()


async def test_the_gate_is_in_the_path() -> None:
    """The old backtester imported strategies, sizing and exits but not the
    gate, so cooldowns, max_open_positions, duplicate-ticker rejection, halt
    and session state played no part in any result (#12)."""
    result = await _run(
        {"SBER": _bars("SBER", ["100"] * 6)},
        (_AlwaysBuy(),),
        config=_config(max_open_positions=0, watchlist=("SBER",)),
    )
    assert result.trades == (), "MAX_POSITIONS=0 must block every entry"


async def test_capital_contention_rejects_the_second_signal() -> None:
    """One ticker and one position made this impossible to model at all."""
    bars = {
        "SBER": _bars("SBER", ["100"] * 6),
        "GAZP": _bars("GAZP", ["100"] * 6),
    }
    lean = _config(
        watchlist=("SBER", "GAZP"),
        allocated_capital=Decimal("150"),
        position_size_pct=Decimal("90"),
        max_open_positions=10,
    )
    result = await _run(bars, (_AlwaysBuy(),), config=lean)
    # Not "only one ticker ever traded" — with four cycles a bar there is time
    # for one to exit and the other to enter on the freed cash, which is
    # correct. What contention means is that two were never open at once.
    spans = sorted(
        (trade.entry_at, trade.exit_at)
        for trade in result.trades
        if trade.exit_at is not None
    )
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] <= later[0], f"overlapping positions: {earlier} {later}"


async def test_the_daily_loss_limit_can_fire_at_all() -> None:
    """One cycle per bar wrote the opening snapshot and measured against it in
    the same instant, so the intra-day loss was always zero and the limit was
    structurally unreachable (#53)."""
    import zarabot.state.halt as halt_mod

    # A bar that opens at 100 and trades down to 40 intraday.
    bars = [
        Candle(
            timestamp=DAY0 + timedelta(days=n),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("40") if n == 2 else Decimal("99"),
            close=Decimal("100") if n != 2 else Decimal("99"),
            volume=1000,
        )
        for n in range(6)
    ]
    halted: list[str] = []
    original = halt_mod.halt

    async def _spy(reason, detail, at):  # type: ignore[no-untyped-def]
        halted.append(reason.value)
        return await original(reason, detail, at)

    import zarabot.app.loops as loops

    real = loops.halt
    loops.halt = _spy  # type: ignore[assignment]
    try:
        await _run(
            {"SBER": bars},
            (_AlwaysBuy(),),
            config=_config(
                watchlist=("SBER",),
                position_size_pct=Decimal("90"),
                daily_loss_limit_pct=Decimal("5"),
                stop_loss_pct=Decimal("80"),
            ),
        )
    finally:
        loops.halt = real  # type: ignore[assignment]
    assert "DAILY_LOSS_LIMIT" in halted, "the limit must be reachable"


async def test_max_age_can_fire_at_all() -> None:
    """lifecycle.exits needs in_closing_window, and the single cycle sat at the
    session start: twenty flat bars with max_holding_days=1 gave zero exits."""
    flat = _bars("SBER", ["100"] * 12)
    result = await _run(
        {"SBER": flat},
        (_AlwaysBuy(),),
        config=_config(
            watchlist=("SBER",),
            max_holding_days=1,
            stop_loss_pct=Decimal("50"),
            take_profit_pct=Decimal("50"),
            reentry_cooldown_minutes=1,
        ),
    )
    triggers = {trigger for trigger, _ in result.exit_trigger_distribution}
    assert ExitTrigger.MAX_AGE in triggers


async def test_a_local_stop_fires_on_the_bar_low() -> None:
    """get_last_price returned the bar's close, so a LOCAL stop was checked
    against the close only — the same optimism the exchange-stop rule removes,
    still present where the bot owns the stop."""
    bars = [
        Candle(
            timestamp=DAY0 + timedelta(days=n),
            open=Decimal("100"),
            high=Decimal("101"),
            # Day 3 trades down to 90 but closes back at 99.
            low=Decimal("90") if n == 3 else Decimal("99"),
            close=Decimal("99"),
            volume=1000,
        )
        for n in range(6)
    ]
    # Refuse stop orders so the position stays LOCAL and the bot owns the
    # trigger — otherwise the exchange stop fires and this proves nothing.
    result = await _run(
        {"SBER": bars},
        (_AlwaysBuy(),),
        config=_config(watchlist=("SBER",), stop_loss_pct=Decimal("5")),
        reject_stops=True,
    )
    triggers = {trigger for trigger, _ in result.exit_trigger_distribution}
    assert ExitTrigger.STOP_LOSS in triggers, "a 5% stop with a low of 90"


async def test_the_opening_snapshot_is_written_once_a_day() -> None:
    """Four cycles must not move the baseline the intra-day loss is measured
    against."""
    import zarabot.app.loops as loops

    written: list[object] = []
    real = loops.write_daily

    async def _spy(snapshot: object) -> None:
        written.append(snapshot)
        return await real(snapshot)  # type: ignore[arg-type]

    loops.write_daily = _spy  # type: ignore[assignment]
    try:
        await _run({"SBER": _bars("SBER", ["100"] * 4)}, (_NeverBuy(),))
    finally:
        loops.write_daily = real  # type: ignore[assignment]
    assert len(written) == 4, "one per Moscow date, not one per cycle"


async def test_drawdown_is_marked_to_market_not_read_off_cash() -> None:
    """`equity` held cash only, appended on trade events. Cash FALLS when you
    buy, so the reported drawdown was roughly the position size (#12)."""
    falling = _bars("SBER", ["100", "100", "98", "96", "94", "92"])
    result = await _run({"SBER": falling}, (_AlwaysBuy(),))
    assert result.max_drawdown > 0
    # A cash-based figure would be ~the position size, i.e. ~10% of capital.
    # A marked-to-market one tracks the instrument's fall, which is far less.
    assert result.max_drawdown < Decimal("9")


async def test_no_real_broker_call_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every seam must be covered. A missed one reaches the network, and that
    is precisely how #39, #43 and the reverted #45 attempt survived."""
    import zarabot.broker.client as client

    async def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a real broker call escaped the simulator")

    monkeypatch.setattr(client, "_connect", _boom)
    monkeypatch.setattr(client, "_open", _boom)
    result = await _run({"SBER": _bars("SBER", ["100"] * 6)}, (_AlwaysBuy(),))
    assert isinstance(result, BacktestResult)


def test_the_seam_table_covers_every_module_that_alerts() -> None:
    """Structural, not behavioural, and deliberately so.

    The obvious test — trip the loss limit and assert nothing was sent — cannot
    work: one bar is one cycle and one Moscow date, so the opening snapshot is
    written and measured in the same instant and the daily loss is always zero.
    `state.halt` is unreachable on that path, and a behavioural guard would pass
    while the seam stayed open. Comparing the table against the source cannot be
    defeated by a path not being reached (#49).
    """
    import ast
    import pathlib
    import re

    importers = set()
    for path in pathlib.Path("zarabot").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "zarabot.telegram.notifier"
                and any(a.name == "alert" for a in node.names)
            ):
                importers.add(str(path)[:-3].replace("/", "."))

    table = pathlib.Path("sandbox/backtest.py").read_text()
    patched = {
        module
        for module, attr in re.findall(r'"(zarabot\.[a-z_.]+)",\s*\n?\s*"(\w+)"', table)
        if attr == "alert"
    }
    missing = importers - patched
    assert not missing, f"modules that alert but are not patched: {sorted(missing)}"


async def test_no_real_alert_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half: nothing reaches the notifier on the ordinary path."""
    import zarabot.telegram.notifier as notifier

    sent: list[str] = []

    async def _record(text: str, urgent: bool) -> None:
        sent.append(text)

    monkeypatch.setattr(notifier, "_send", _record)
    await _run(
        {"SBER": _bars("SBER", ["100", "98", "96", "94", "92", "90"])}, (_AlwaysBuy(),)
    )
    assert sent == [], f"a real alert escaped the simulator: {sent}"


async def test_no_unpatched_clock_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated time must be total. A module still reading the wall clock
    would date a backtest's rows to today."""
    import zarabot.clock as clock

    def _boom() -> object:
        raise AssertionError("an unpatched clock read escaped the simulator")

    monkeypatch.setattr(clock, "now", _boom)
    result = await _run({"SBER": _bars("SBER", ["100"] * 6)}, (_AlwaysBuy(),))
    assert isinstance(result, BacktestResult)


async def test_no_configuration_is_loaded_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backtest's Config is the one the whole run must see."""
    import zarabot.config as config

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("configuration was loaded from the environment")

    monkeypatch.setattr(config, "load", _boom)
    monkeypatch.setattr(config, "get", _boom)
    result = await _run({"SBER": _bars("SBER", ["100"] * 6)}, (_AlwaysBuy(),))
    assert isinstance(result, BacktestResult)
