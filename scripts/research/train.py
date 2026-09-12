#!/usr/bin/env python3
"""Train and export the ML model from the command line.

`sandbox.train.fit` and `export` were reachable only from the test suite (#212),
so every model that has ever existed came from a notebook cell nobody kept. The
seed is recorded in the bundle precisely so a model can be audited after a losing
week; that is worth nothing if the rest of the invocation — the tickers, the date
range, the horizon, the fold count — is not written down anywhere.

This wires, it does not decide. Features come from
`strategies.ml_model.build_features` through `sandbox.train`, which has the one
implementation of them; the candles come from `sandbox.data.load` and its parquet
cache; the validation is `fit`'s walk-forward, never a split made here.

Needs the live environment: `config` owns configuration, `fit` reads the stop and
target percentages from it, and `sandbox.data.load` fetches through
`broker.client` on a cache miss.

    set -a && . ./.env && set +a && .venv/bin/python scripts/research/train.py \
        --start 2023-01-01 --end 2026-01-01 --seed 7

The bundle lands under `sandbox/models/`, which is gitignored: a model file is
never committed, and `strategies.ml_model.load` reads it from `ML_MODEL_PATH`.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections.abc import Sequence
from datetime import datetime

# Run as a file, not a package: `python scripts/research/train.py` puts this
# directory on sys.path, never the repository root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from t_tech.invest.schemas import CandleInterval  # noqa: E402

from sandbox.data import load  # noqa: E402
from sandbox.train import FittedModel, export, fit  # noqa: E402
from zarabot import config  # noqa: E402
from zarabot.models import Candle  # noqa: E402

_INTERVAL = CandleInterval.CANDLE_INTERVAL_DAY
_DEFAULT_OUT = pathlib.Path("sandbox/models/model.joblib")


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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_day, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=_day, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--seed",
        type=int,
        required=True,
        help="required and recorded in the bundle: an unreproducible model "
        "cannot be audited after a losing week",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="defaults to WATCHLIST from the environment",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=15,
        help="days the label looks ahead for target-before-stop",
    )
    parser.add_argument(
        "--folds", type=int, default=3, help="walk-forward validation folds"
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
        default=_DEFAULT_OUT,
        help="where to write the joblib bundle (sandbox/models/ is gitignored)",
    )
    return parser.parse_args(argv)


async def gather(
    tickers: Sequence[str],
    start: datetime,
    end: datetime,
    cache_dir: pathlib.Path,
) -> dict[str, list[Candle]]:
    """Cached candles per ticker, omitting the tickers that have none."""
    out: dict[str, list[Candle]] = {}
    for ticker in tickers:
        candles = await load(ticker, start, end, _INTERVAL, cache_dir)
        if not candles:
            print(f"   {ticker:<6} no candles in range — dropped from training")
            continue
        out[ticker] = candles
        print(
            f"   {ticker:<6} {len(candles):>5} candles  "
            f"{candles[0].timestamp.date()} → {candles[-1].timestamp.date()}"
        )
    return out


def summary(model: FittedModel, path: pathlib.Path) -> str:
    """Per fold, never averaged: one number hides a model that works in one
    regime and fails in another."""
    folds = "\n".join(
        f"     fold {index + 1}          {score}"
        for index, score in enumerate(model.fold_scores)
    )
    return "\n".join(
        [
            f"   seed              {model.seed}",
            f"   trained at        {model.trained_at.isoformat()}",
            f"   features          {', '.join(model.features)}",
            f"   fold scores       {len(model.fold_scores)} folds",
            folds,
            f"   bundle            {path}",
        ]
    )


async def body(args: argparse.Namespace) -> int:
    cfg = config.get()
    tickers = list(args.tickers) if args.tickers else list(cfg.watchlist)
    print("zarabot model training — walk-forward validation")
    print(f"   range             {args.start.date()} → {args.end.date()}")
    print(f"   horizon           {args.horizon_days} days")
    print(f"   folds             {args.folds}")
    print(f"   seed              {args.seed}")
    # The label's stop and target percentages are not printed here because they
    # are not this script's to state: `fit` reads them from `config`, and a
    # second rendering of a number owned elsewhere is a number that can drift.
    print("\ncandles")
    candles_by_ticker = await gather(tickers, args.start, args.end, args.cache_dir)
    if not candles_by_ticker:
        print("\nno candles for any ticker in this range — nothing to train on")
        return 1

    model = fit(
        candles_by_ticker=candles_by_ticker,
        horizon_days=args.horizon_days,
        folds=args.folds,
        seed=args.seed,
    )
    path = export(model, args.out)
    print("\nmodel")
    print(summary(model, path))
    return 0


def main() -> int:
    return asyncio.run(body(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
