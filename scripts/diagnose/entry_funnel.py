#!/usr/bin/env python3
"""Why has the bot not entered a position? Answers it stage by stage.

Read-only. Calls `get_instrument`, `get_candles` and `get_portfolio`, and
nothing that places, amends or cancels an order. The only write it makes is the
one `market.session.refresh` always makes — the fourteen-day window into
`db.trading_days` — because `is_open` is meaningless without a
populated calendar and that fetch is the seam #39 lived in.

It imports the live modules and calls them unchanged. A diagnostic that
recomputes an SMA to explain why the SMA did not cross is measuring itself:
every strategy verdict below comes from the shipped strategy object, every
decision from `risk.gate.check`, and the candles from
`market.data.candles_for_watchlist` — the same call `app.loops` makes, with the
same lookback.

Seven stages, in the order the trading cycle runs them. The first one that
stops is the answer:

    1. configuration      what the bot was told to trade
    2. session            is_open, and the calendar behind it
    3. halt               a latched halt blocks every entry
    4. market data        the candles the bot itself would fetch, per ticker
    5. strategies         a verdict now, and a replay over real history
    6. gate               the real gate and sizer on any signal that fired
    7. recorded evidence  what the database says has happened so far

Run it on the VPS, inside the app image, with the live environment:

    cd /opt/zarabot/app && docker compose -f docker-compose.deploy.yml run \
        --rm --no-deps -v "$PWD/scripts:/app/scripts" zarabot \
        python scripts/diagnose/entry_funnel.py

Or from a checkout with .env and the virtualenv:

    set -a && . ./.env && set +a && .venv/bin/python scripts/diagnose/entry_funnel.py
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections.abc import Awaitable
from datetime import datetime, timedelta
from decimal import Decimal

# Run as a file, not a package: `python scripts/diagnose/entry_funnel.py` puts
# this directory on sys.path, never the repository root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from t_tech.invest.schemas import CandleInterval  # noqa: E402

from zarabot import clock, config  # noqa: E402
from zarabot.broker.client import (  # noqa: E402
    get_candles,
    get_instrument,
    get_portfolio,
)
from zarabot.db import connection  # noqa: E402
from zarabot.db.cooldowns import is_active  # noqa: E402
from zarabot.db.positions import list_closed, list_open  # noqa: E402
from zarabot.db.signals import list_for_period  # noqa: E402
from zarabot.market.data import candles_for_watchlist  # noqa: E402
from zarabot.market.session import (  # noqa: E402
    calendar,
    covers,
    current_session,
    is_open,
    refresh,
)
from zarabot.models import Candle, Instrument, PortfolioState, Signal  # noqa: E402
from zarabot.risk.gate import check  # noqa: E402
from zarabot.state.halt import current as halt_state  # noqa: E402
from zarabot.strategies.base import Strategy  # noqa: E402
from zarabot.strategies.registry import enabled  # noqa: E402

_SCHEDULE_DAYS = 14
_MSK = "%Y-%m-%d %H:%M MSK"


def _msk(moment: datetime | None) -> str:
    return "—" if moment is None else clock.to_moscow(moment).strftime(_MSK)


def _day(moment: datetime) -> str:
    return clock.to_moscow(moment).strftime("%Y-%m-%d")


def _head(number: int, title: str) -> None:
    print(f"\n{number}. {title}\n{'-' * (len(title) + 3)}")


_secrets: list[str] = []


def _register_secret(value: str) -> None:
    if value and len(value) >= 8:
        _secrets.append(value)


def _redact(text: object) -> str:
    out = str(text)
    for secret in _secrets:
        out = out.replace(secret, "***REDACTED***")
    return out


async def _guard[T](coro: Awaitable[T]) -> T | None:
    """Run one stage. A stage that fails reports and the rest still run."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
        print(f"   ** stage failed — {type(exc).__name__}: {_redact(exc)} **")
        return None


def _stage_config(cfg: config.Config) -> None:
    _head(1, "configuration")
    print(f"   mode              {cfg.trading_mode}")
    print(f"   watchlist         {', '.join(cfg.watchlist)}")
    print(f"   strategies        {', '.join(cfg.enabled_strategies)}")
    print(f"   allocated capital {cfg.allocated_capital}")
    print(
        f"   position size     {cfg.position_size_pct}%  reserve "
        f"{cfg.cash_reserve_pct}%  max open {cfg.max_open_positions}"
    )
    print(f"   cooldown          {cfg.reentry_cooldown_minutes} min")
    print(f"   poll interval     {cfg.poll_interval_seconds}s")
    lower = [t for t in cfg.watchlist if t != t.upper()]
    if lower:
        print(
            f"   ** WATCHLIST is not upper case: {', '.join(lower)} — "
            "share_by is case sensitive and these resolve to nothing **"
        )


async def _stage_session(moment: datetime) -> bool:
    _head(2, "session")
    await refresh(_SCHEDULE_DAYS)
    cal = calendar()
    days = list(cal.sessions)
    trading = [s for s in days if s.is_trading_day]
    print(
        f"   calendar          {len(days)} days recorded, "
        f"{len(trading)} of them trading"
    )
    if trading:
        print(
            f"   first / last      {_msk(trading[0].start)} … {_msk(trading[-1].end)}"
        )
    today = current_session(moment)
    if today is None:
        print("   today             no session in the calendar for now")
    else:
        print(
            f"   today             {_msk(today.start)} … {_msk(today.end)}  "
            f"trading_day={today.is_trading_day}"
        )
    print(f"   covers(today)     {covers(clock.moscow_date(moment))}")
    open_now = is_open(moment)
    print(f"   is_open(now)      {open_now}")
    if not open_now:
        print(
            "   ** entries only run while is_open is true. Everything below "
            "still measures, but the bot would have stopped here. **"
        )
    return open_now


async def _stage_halt() -> bool:
    _head(3, "halt")
    state = await halt_state()
    if state is None or not state.halted:
        print("   halted            False")
        return False
    print(f"   halted            True — {state.reason} {state.detail}")
    print(f"   since             {_msk(state.halted_at)}")
    print(
        "   ** a latched halt blocks every entry until /resume. This is the "
        "answer; nothing below matters until it is cleared. **"
    )
    return True


async def _stage_data(
    cfg: config.Config, strategies: list[Strategy], moment: datetime
) -> dict[str, list[Candle]]:
    _head(4, "market data — the bot's own fetch")
    lookback = max((s.lookback for s in strategies), default=0)
    print(
        f"   lookback needed   {lookback} candles "
        f"(longest of {', '.join(s.name for s in strategies)})"
    )
    candles = await candles_for_watchlist(list(cfg.watchlist), lookback, moment)
    for ticker in cfg.watchlist:
        series = candles.get(ticker)
        if not series:
            print(
                f"   {ticker:<6} ** NO CANDLES — market.data dropped this "
                "ticker with a warning and no alert (#23). The bot is blind "
                "to it and can never signal on it. **"
            )
            continue
        short = (
            " ** SHORT: below the lookback, every strategy returns None **"
            if len(series) < lookback
            else ""
        )
        print(
            f"   {ticker:<6} {len(series):>4} candles  "
            f"{_day(series[0].timestamp)} → {_day(series[-1].timestamp)}  "
            f"last close {series[-1].close}{short}"
        )
    return candles


def _replay(strategy: Strategy, ticker: str, series: list[Candle]) -> list[str]:
    """Signal days the shipped strategy object produces over real history."""
    days: list[str] = []
    for end in range(strategy.lookback, len(series) + 1):
        window = series[:end]
        signal = strategy.evaluate(ticker, window, window[-1].timestamp)
        if signal is not None:
            days.append(_day(window[-1].timestamp))
    return days


async def _stage_strategies(
    cfg: config.Config,
    strategies: list[Strategy],
    live: dict[str, list[Candle]],
    moment: datetime,
    history_days: int,
) -> list[Signal]:
    _head(5, "strategies")
    fired: list[Signal] = []
    print("   verdict now, on the candles from stage 4:")
    if not any(live.get(ticker) for ticker in cfg.watchlist):
        print("     no ticker had candles, so no strategy was even consulted")
    for ticker in cfg.watchlist:
        series = live.get(ticker)
        if not series:
            continue
        for strategy in strategies:
            signal = strategy.evaluate(ticker, series, moment)
            mark = "SIGNAL" if signal is not None else "  —   "
            print(f"     {mark}  {ticker:<6} {strategy.name}")
            if signal is not None:
                fired.append(signal)

    print(
        f"\n   replay over the last {history_days} calendar days of real "
        "candles, one evaluation per completed bar:"
    )
    since = moment - timedelta(days=history_days)
    total = 0
    unavailable = 0
    for ticker in cfg.watchlist:
        try:
            instrument = await get_instrument(ticker)
            series = await get_candles(
                instrument.figi, CandleInterval.CANDLE_INTERVAL_DAY, since, moment
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
            unavailable += 1
            print(
                f"     {ticker:<6} history unavailable — "
                f"{type(exc).__name__}: {_redact(exc)}"
            )
            continue
        for strategy in strategies:
            days = _replay(strategy, ticker, series)
            total += len(days)
            last = f"last {days[-1]}" if days else "never"
            print(
                f"     {ticker:<6} {strategy.name:<14} "
                f"{len(days):>3} signal-days over {len(series)} bars, {last}"
            )
    if unavailable:
        print(
            f"\n   {unavailable} of {len(cfg.watchlist)} tickers could not be "
            "replayed, so this count measures nothing yet. The history fetch "
            "failing here is itself the finding: the bot's own candle fetch "
            "fails the same way, silently, every cycle."
        )
        return fired
    print(
        f"\n   {total} signal-days in total. Zero here is a strategy finding, "
        "not a plumbing one: the rules simply do not fire on this watchlist. "
        "A healthy number here with an empty signals table in stage 7 means "
        "the bot is not reaching its strategies at all."
    )
    return fired


async def _stage_gate(
    cfg: config.Config,
    fired: list[Signal],
    halted: bool,
    open_now: bool,
    moment: datetime,
) -> None:
    _head(6, "gate — the real risk.gate, dry run")
    portfolio: PortfolioState = await get_portfolio()
    print(f"   broker cash       {portfolio.cash}")
    print(f"   broker positions  {len(portfolio.positions)}")
    if not fired:
        print(
            "   no signal fired this instant, so the gate had nothing to "
            "judge. Stage 5's replay says whether that is normal."
        )
        return
    for signal in fired:
        instrument: Instrument = await get_instrument(signal.ticker)
        cooldown = await is_active(signal.ticker, moment, cfg.reentry_cooldown_minutes)
        print(
            f"   {signal.ticker} / {signal.strategy}: lot={instrument.lot} "
            f"status={instrument.trading_status} price={signal.reference_price} "
            f"lot cost={Decimal(instrument.lot) * signal.reference_price} "
            f"cooldown={cooldown}"
        )
        for label, session_open in _gate_cases(open_now):
            decision = check(
                signal,
                portfolio,
                instrument,
                cooldown,
                session_open,
                halted,
                moment,
                cfg,
            )
            verdict = (
                f"APPROVED {decision.lots} lots"
                if decision.approved
                else f"rejected {decision.reason}"
            )
            print(f"     {label:<22} {verdict}")


def _gate_cases(open_now: bool) -> list[tuple[str, bool]]:
    if open_now:
        return [("decision", True)]
    return [("decision now", False), ("if session were open", True)]


async def _stage_evidence(moment: datetime, days: int) -> None:
    _head(7, "recorded evidence")
    start = clock.moscow_date(moment - timedelta(days=days))
    end = clock.moscow_date(moment)
    rows = await list_for_period(start, end)
    print(f"   signals recorded  {len(rows)} in the last {days} days")
    reasons: dict[str, int] = {}
    for _signal, decision in rows:
        key = (
            "APPROVED"
            if decision.approved
            else str(decision.reason.value if decision.reason else "REJECTED")
        )
        reasons[key] = reasons.get(key, 0) + 1
    for key, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"     {key:<24} {count}")
    print(f"   positions open    {len(await list_open())}")
    print(f"   positions closed  {len(await list_closed())}")
    if not rows:
        print(
            "   ** an empty signals table means no strategy has fired since "
            "this database was created — it does NOT mean an entry was "
            "rejected. Read it against stage 5's replay. **"
        )


async def _body(args: argparse.Namespace) -> None:
    cfg = config.get()
    _register_secret(cfg.tinvest_token)
    moment = clock.now()
    print("zarabot entry-funnel diagnosis — read-only, places no orders")
    print(f"now {_msk(moment)}   db {cfg.db_path}")

    await connection.connect(str(cfg.db_path))
    try:
        # Every stage is guarded. A diagnostic that dies at stage 3 answers
        # nothing about stages 4 to 7, and the stage that fails is itself a
        # finding worth printing beside the ones that did not.
        _stage_config(cfg)
        open_now = bool(await _guard(_stage_session(moment)))
        halted = bool(await _guard(_stage_halt()))
        strategies = enabled(cfg)
        live = await _guard(_stage_data(cfg, strategies, moment)) or {}
        fired = (
            await _guard(
                _stage_strategies(cfg, strategies, live, moment, args.history_days)
            )
            or []
        )
        await _guard(_stage_gate(cfg, fired, halted, open_now, moment))
        await _guard(_stage_evidence(moment, args.evidence_days))
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-days",
        type=int,
        default=400,
        help="calendar days of candles for the strategy replay",
    )
    parser.add_argument(
        "--evidence-days",
        type=int,
        default=30,
        help="how far back to read recorded signals",
    )
    args = parser.parse_args()
    asyncio.run(_body(args))


if __name__ == "__main__":
    main()
