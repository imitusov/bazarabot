#!/usr/bin/env python3
"""Run a backtest from the command line, with its arguments recorded.

`sandbox.backtest.run` was reachable only from the test suite (#212): no
`__main__`, no script, no Makefile target. Running a backtest therefore meant
hand-writing a driver that loaded candles, built an `Instrument` map, a `Config`
and a `Commission` — a driver that lived in nobody's repository, so no two
backtests were comparable and no result was recorded anywhere.

This is that driver, under version control. It **wires**, it does not decide:
every strategy object comes from `strategies.registry.enabled`, the gate, the
sizing and the exits are reached by `sandbox.backtest.run` driving
`app.loops.trading_cycle` itself, and the candles come from `sandbox.data.load`,
which caches to parquet so a re-run is cheap and identical. Nothing about a
strategy, a size or an exit is computed here; a runner that re-derived any of
them would be measuring itself.

Needs the live environment, because `config` owns configuration and
`sandbox.data.load` fetches through `broker.client` on a cache miss — a warm
`sandbox/cache/` makes the fetch unnecessary, not the configuration.

    set -a && . ./.env && set +a && .venv/bin/python scripts/research/backtest.py \
        --start 2025-01-01 --end 2026-01-01 --out sandbox/results/2026-01.json

The tariff is an argument rather than a default guess: commission is never
estimated by the trading path, and a backtest that invented one would be
reporting a fee the broker does not charge. `--slippage` is the assumption named
in the spec's fill model, stated per run and recorded with the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

# Run as a file, not a package: `python scripts/research/backtest.py` puts this
# directory on sys.path, never the repository root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from t_tech.invest.schemas import CandleInterval  # noqa: E402

from sandbox.backtest import run  # noqa: E402
from sandbox.data import load  # noqa: E402
from sandbox.exchange import Commission  # noqa: E402
from zarabot import config  # noqa: E402
from zarabot.broker.client import get_instrument  # noqa: E402
from zarabot.clock import now as clock_now  # noqa: E402
from zarabot.models import BacktestResult, Candle, Instrument  # noqa: E402
from zarabot.strategies.registry import enabled  # noqa: E402

_INTERVAL = CandleInterval.CANDLE_INTERVAL_DAY

# The tariff shape sandbox.exchange models: a percentage of turnover with a
# minimum. Stated as defaults so a run is reproducible from its recorded
# arguments, never as an estimate standing in for the broker's own figure.
_DEFAULT_COMMISSION_PCT = "0.05"
_DEFAULT_COMMISSION_MIN = "1"


def _day(text: str) -> datetime:
    """A `YYYY-MM-DD` argument as midnight UTC.

    The offset is appended and parsed rather than attached afterwards, so the
    result is aware by construction: `sandbox.data.load` raises `ValueError` on
    a naive datetime, and nothing in this project may guess a timezone.
    """
    try:
        return datetime.strptime(f"{text}+0000", "%Y-%m-%d%z")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from exc


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_day, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=_day, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="defaults to WATCHLIST from the environment",
    )
    parser.add_argument(
        "--slippage",
        type=_decimal,
        default=Decimal("0"),
        help="percent applied to every simulated fill",
    )
    parser.add_argument(
        "--commission-pct",
        type=_decimal,
        default=Decimal(_DEFAULT_COMMISSION_PCT),
        help="percent of turnover, per fill",
    )
    parser.add_argument(
        "--commission-min",
        type=_decimal,
        default=Decimal(_DEFAULT_COMMISSION_MIN),
        help="minimum fee per fill",
    )
    parser.add_argument(
        "--reject-stops",
        action="store_true",
        help="make the exchange refuse stop orders (rule 23's LOCAL degrade path)",
    )
    parser.add_argument(
        "--cache-dir",
        type=pathlib.Path,
        default=pathlib.Path("sandbox/cache"),
        help="parquet candle cache",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=None,
        help="write the arguments and the result as JSON to this path",
    )
    return parser.parse_args(argv)


async def gather(
    tickers: Sequence[str],
    start: datetime,
    end: datetime,
    cache_dir: pathlib.Path,
) -> tuple[dict[str, list[Candle]], dict[str, Instrument]]:
    """Cached bars and their instruments, for the tickers that have candles.

    A ticker with no candles is left out rather than passed as an empty series:
    an instrument with no bars is a hole the simulated exchange would have to
    guess at, and the printed report says which tickers were dropped.
    """
    bars: dict[str, list[Candle]] = {}
    instruments: dict[str, Instrument] = {}
    for ticker in tickers:
        candles = await load(ticker, start, end, _INTERVAL, cache_dir)
        if not candles:
            print(f"   {ticker:<6} no candles in range — dropped from the run")
            continue
        bars[ticker] = candles
        instruments[ticker] = await get_instrument(ticker)
        print(
            f"   {ticker:<6} {len(candles):>5} candles  "
            f"{candles[0].timestamp.date()} → {candles[-1].timestamp.date()}"
        )
    return bars, instruments


def summary(result: BacktestResult) -> str:
    """The result, as the numbers it carries. No derived judgements."""
    triggers = (
        ", ".join(
            f"{trigger.value}={count}"
            for trigger, count in result.exit_trigger_distribution
        )
        or "none"
    )
    benchmark = "—" if result.benchmark_return is None else str(result.benchmark_return)
    return "\n".join(
        [
            f"   trades            {len(result.trades)}",
            f"   realised pnl      {result.pnl}",
            f"   win rate          {result.win_rate}",
            f"   max drawdown      {result.max_drawdown} %",
            f"   exit triggers     {triggers}",
            f"   benchmark return  {benchmark}",
        ]
    )


def record(
    *,
    args: argparse.Namespace,
    tickers: Sequence[str],
    strategies: Sequence[object],
    cfg: config.Config,
    result: BacktestResult,
) -> dict[str, object]:
    """What was run and what came out, as JSON-safe values.

    The configuration is copied field by field rather than wholesale: `Config`
    holds two tokens, and a recorded run is a file somebody will paste.
    """
    return {
        "run_at": clock_now().isoformat(),
        "tickers": list(tickers),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "interval": _INTERVAL.name,
        "slippage": str(args.slippage),
        "commission_pct": str(args.commission_pct),
        "commission_minimum": str(args.commission_min),
        "reject_stops": bool(args.reject_stops),
        "strategies": [str(getattr(s, "name", s)) for s in strategies],
        "config": {
            "allocated_capital": str(cfg.allocated_capital),
            "position_size_pct": str(cfg.position_size_pct),
            "stop_loss_pct": str(cfg.stop_loss_pct),
            "take_profit_pct": str(cfg.take_profit_pct),
            "max_holding_days": cfg.max_holding_days,
            "max_open_positions": cfg.max_open_positions,
            "reentry_cooldown_minutes": cfg.reentry_cooldown_minutes,
            "daily_loss_limit_pct": str(cfg.daily_loss_limit_pct),
            "cash_reserve_pct": str(cfg.cash_reserve_pct),
        },
        "result": {
            "trades": len(result.trades),
            "pnl": str(result.pnl),
            "win_rate": str(result.win_rate),
            "max_drawdown": str(result.max_drawdown),
            "exit_triggers": {
                trigger.value: count
                for trigger, count in result.exit_trigger_distribution
            },
            "benchmark_return": (
                None
                if result.benchmark_return is None
                else str(result.benchmark_return)
            ),
        },
    }


async def body(args: argparse.Namespace) -> int:
    cfg = config.get()
    tickers = list(args.tickers) if args.tickers else list(cfg.watchlist)
    strategies = enabled(cfg)
    print("zarabot backtest — simulated exchange, temporary database")
    print(f"   range             {args.start.date()} → {args.end.date()}")
    print(f"   strategies        {', '.join(s.name for s in strategies)}")
    print(
        f"   commission        {args.commission_pct}% of turnover, "
        f"minimum {args.commission_min}"
    )
    print(f"   slippage          {args.slippage}%")
    print("\ncandles")
    bars, instruments = await gather(tickers, args.start, args.end, args.cache_dir)
    if not bars:
        print("\nno candles for any ticker in this range — nothing to replay")
        return 1

    result = await run(
        bars=bars,
        instruments=instruments,
        config=cfg,
        strategies=strategies,
        commission=Commission(pct=args.commission_pct, minimum=args.commission_min),
        slippage=args.slippage,
        reject_stops=args.reject_stops,
    )
    print("\nresult")
    print(summary(result))

    if args.out is not None:
        payload = record(
            args=args,
            tickers=list(bars),
            strategies=strategies,
            cfg=cfg,
            result=result,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nrecorded to {args.out}")
    return 0


def main() -> int:
    return asyncio.run(body(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
