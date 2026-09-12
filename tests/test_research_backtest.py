"""Tests for scripts/research/backtest.py — the backtest entrypoint (#212).

`sandbox.backtest.run` was reachable only from this suite: no `__main__`, no
script, no Makefile target. That is failure class 10 applied to the research
path, so these tests pin the one thing a runner must be: a command that drives
the *live* modules — the registry's strategy objects, the candles
`sandbox.data.load` cached, the `Config` the bot itself reads — with its
arguments recorded, and nothing re-derived on the way.

Loaded the way `tests/test_export_health.py` loads its script: by path, because
`scripts/` is not a package.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from zarabot.models import BacktestResult, Candle, ExitTrigger, Instrument

DAY0 = datetime(2026, 3, 16, 7, 0, tzinfo=UTC)
NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


def _mod() -> ModuleType:
    path = Path("scripts/research/backtest.py")
    spec = importlib.util.spec_from_file_location("research_backtest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "watchlist": ("SBER", "GAZP"),
        "enabled_strategies": ("ma_crossover",),
        "allocated_capital": Decimal("100000"),
        "position_size_pct": Decimal("10"),
        "stop_loss_pct": Decimal("5"),
        "take_profit_pct": Decimal("10"),
        "max_holding_days": 3,
        "max_open_positions": 5,
        "reentry_cooldown_minutes": 120,
        "daily_loss_limit_pct": Decimal("5"),
        "cash_reserve_pct": Decimal("1"),
        "tinvest_token": "SECRET-TOKEN-VALUE",
        "telegram_bot_token": "SECRET-TELEGRAM-TOKEN",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _candles(count: int) -> list[Candle]:
    out: list[Candle] = []
    for n in range(count):
        price = Decimal("100") + Decimal(n)
        out.append(
            Candle(
                timestamp=DAY0 + timedelta(days=n),
                open=price,
                high=price,
                low=price,
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


def _result(**overrides: object) -> BacktestResult:
    fields: dict[str, object] = {
        "trades": (),
        "pnl": Decimal("1234.50"),
        "win_rate": Decimal("0.5"),
        "max_drawdown": Decimal("7.25"),
        "exit_trigger_distribution": ((ExitTrigger.STOP_LOSS, 2),),
        "benchmark_return": Decimal("0.03"),
    }
    fields.update(overrides)
    return BacktestResult(**fields)  # type: ignore[arg-type]


class _Strategy:
    """A stand-in for whatever `strategies.registry.enabled` hands back."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lookback = 2


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The script with every seam replaced: no network, no clock, no env."""
    mod = _mod()
    cfg = _config()
    strategies = [_Strategy("ma_crossover")]
    calls: dict[str, Any] = {"load": [], "instruments": [], "run": None}

    async def _load(
        ticker: str,
        start: datetime,
        end: datetime,
        interval: object,
        cache_dir: Path = Path("sandbox/cache"),
    ) -> list[Candle]:
        calls["load"].append((ticker, start, end, interval, cache_dir))
        return _candles(4)

    async def _get_instrument(ticker: str) -> Instrument:
        calls["instruments"].append(ticker)
        return _instrument(ticker)

    async def _run(**kwargs: object) -> BacktestResult:
        calls["run"] = kwargs
        return _result()

    monkeypatch.setattr(mod, "load", _load)
    monkeypatch.setattr(mod, "get_instrument", _get_instrument)
    monkeypatch.setattr(mod, "run", _run)
    monkeypatch.setattr(mod, "enabled", lambda _cfg: strategies)
    monkeypatch.setattr(mod, "config", SimpleNamespace(get=lambda: cfg))
    monkeypatch.setattr(mod, "clock_now", lambda: NOW)
    return SimpleNamespace(mod=mod, cfg=cfg, strategies=strategies, calls=calls)


def test_dates_reach_sandbox_data_timezone_aware(wired: SimpleNamespace) -> None:
    """`sandbox.data.load` raises on a naive datetime; the CLI takes dates."""
    args = wired.mod.parse_args(["--start", "2026-03-01", "--end", "2026-03-31"])
    assert args.start == datetime(2026, 3, 1, tzinfo=UTC)
    assert args.end == datetime(2026, 3, 31, tzinfo=UTC)
    assert args.start.tzinfo is not None
    assert args.end.tzinfo is not None


def test_a_bad_date_is_rejected_rather_than_guessed() -> None:
    mod = _mod()
    with pytest.raises(SystemExit):
        mod.parse_args(["--start", "16/03/2026", "--end", "2026-03-31"])


async def test_tickers_default_to_the_configured_watchlist(
    wired: SimpleNamespace,
) -> None:
    args = wired.mod.parse_args(["--start", "2026-03-01", "--end", "2026-03-31"])
    code = await wired.mod.body(args)
    assert code == 0
    loaded = [call[0] for call in wired.calls["load"]]
    assert loaded == list(wired.cfg.watchlist)


async def test_run_gets_the_registry_strategy_objects_unchanged(
    wired: SimpleNamespace,
) -> None:
    """The runner must not build its own strategies — that is the whole rule."""
    args = wired.mod.parse_args(
        ["--start", "2026-03-01", "--end", "2026-03-31", "--tickers", "SBER"]
    )
    await wired.mod.body(args)
    passed = wired.calls["run"]["strategies"]
    assert all(
        actual is expected
        for actual, expected in zip(passed, wired.strategies, strict=True)
    )


async def test_run_gets_cached_bars_instruments_and_the_cli_tariff(
    wired: SimpleNamespace,
) -> None:
    args = wired.mod.parse_args(
        [
            "--start",
            "2026-03-01",
            "--end",
            "2026-03-31",
            "--tickers",
            "SBER",
            "GAZP",
            "--slippage",
            "0.2",
            "--commission-pct",
            "0.04",
            "--commission-min",
            "2",
        ]
    )
    await wired.mod.body(args)
    kwargs = wired.calls["run"]
    assert sorted(kwargs["bars"]) == ["GAZP", "SBER"]
    assert all(candle.timestamp.tzinfo is not None for candle in kwargs["bars"]["SBER"])
    assert sorted(kwargs["instruments"]) == ["GAZP", "SBER"]
    assert kwargs["instruments"]["SBER"].figi == "BBG-SBER"
    assert kwargs["config"] is wired.cfg
    assert kwargs["slippage"] == Decimal("0.2")
    assert kwargs["commission"].pct == Decimal("0.04")
    assert kwargs["commission"].minimum == Decimal("2")


async def test_a_ticker_with_no_candles_is_not_passed_as_an_empty_series(
    wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _load(ticker: str, *_args: object, **_kwargs: object) -> list[Candle]:
        return _candles(4) if ticker == "SBER" else []

    monkeypatch.setattr(wired.mod, "load", _load)
    args = wired.mod.parse_args(
        ["--start", "2026-03-01", "--end", "2026-03-31", "--tickers", "SBER", "GAZP"]
    )
    await wired.mod.body(args)
    assert list(wired.calls["run"]["bars"]) == ["SBER"]
    assert "GAZP" not in wired.calls["run"]["instruments"]


async def test_no_candles_at_all_reports_and_does_not_run(
    wired: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _load(*_args: object, **_kwargs: object) -> list[Candle]:
        return []

    monkeypatch.setattr(wired.mod, "load", _load)
    args = wired.mod.parse_args(["--start", "2026-03-01", "--end", "2026-03-31"])
    code = await wired.mod.body(args)
    assert code == 1
    assert wired.calls["run"] is None
    assert "no candles" in capsys.readouterr().out.lower()


def test_summary_reports_every_number_the_result_carries() -> None:
    mod = _mod()
    text = mod.summary(_result())
    assert "1234.50" in text
    assert "7.25" in text
    assert "STOP_LOSS" in text
    assert "0.03" in text


async def test_out_records_the_arguments_beside_the_result(
    wired: SimpleNamespace, tmp_path: Path
) -> None:
    out = tmp_path / "run.json"
    args = wired.mod.parse_args(
        [
            "--start",
            "2026-03-01",
            "--end",
            "2026-03-31",
            "--tickers",
            "SBER",
            "--slippage",
            "0.2",
            "--out",
            str(out),
        ]
    )
    await wired.mod.body(args)
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["tickers"] == ["SBER"]
    assert record["start"] == "2026-03-01T00:00:00+00:00"
    assert record["end"] == "2026-03-31T00:00:00+00:00"
    assert record["slippage"] == "0.2"
    assert record["strategies"] == ["ma_crossover"]
    assert record["run_at"] == NOW.isoformat()
    assert record["result"]["pnl"] == "1234.50"
    assert record["result"]["max_drawdown"] == "7.25"
    assert record["result"]["exit_triggers"] == {"STOP_LOSS": 2}
    assert record["config"]["allocated_capital"] == "100000"


async def test_the_recorded_run_carries_no_token(
    wired: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule: never log a token, in a message or a structured field."""
    out = tmp_path / "run.json"
    args = wired.mod.parse_args(
        ["--start", "2026-03-01", "--end", "2026-03-31", "--out", str(out)]
    )
    await wired.mod.body(args)
    text = out.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "SECRET-TOKEN-VALUE" not in text
    assert "SECRET-TELEGRAM-TOKEN" not in text
