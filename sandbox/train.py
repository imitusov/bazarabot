"""Walk-forward training. Feature construction is owned by strategies.ml_model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
from sklearn.linear_model import LogisticRegression

from zarabot.clock import now
from zarabot.config import load
from zarabot.models import Candle
from zarabot.strategies.ml_model import FEATURE_NAMES, build_features

_LOOKBACK = 11
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class FittedModel:
    estimator: Any
    features: tuple[str, ...]
    seed: int
    trained_at: datetime
    fold_scores: tuple[Decimal, ...]


def _label(
    candles: list[Candle],
    index: int,
    horizon_days: int,
    stop_pct: Decimal,
    target_pct: Decimal,
) -> int:
    entry = candles[index].close
    stop = entry * (_HUNDRED - stop_pct) / _HUNDRED
    target = entry * (_HUNDRED + target_pct) / _HUNDRED
    deadline = candles[index].timestamp + timedelta(days=horizon_days)
    for later in candles[index + 1 :]:
        if later.timestamp > deadline:
            break
        hit_stop = later.low <= stop
        hit_target = later.high >= target
        if hit_stop:
            return 0
        if hit_target:
            return 1
    return 0


def _samples(
    candles_by_ticker: dict[str, list[Candle]],
    horizon_days: int,
    stop_pct: Decimal,
    target_pct: Decimal,
) -> list[tuple[datetime, list[float], int]]:
    rows: list[tuple[datetime, list[float], int]] = []
    for candles in candles_by_ticker.values():
        ordered = sorted(candles, key=lambda candle: candle.timestamp)
        last = len(ordered) - 1
        for index in range(_LOOKBACK - 1, last):
            window = ordered[index - _LOOKBACK + 1 : index + 1]
            try:
                features = build_features(window)
            except ValueError:
                continue
            label = _label(ordered, index, horizon_days, stop_pct, target_pct)
            rows.append((ordered[index].timestamp, features, label))
    rows.sort(key=lambda row: row[0])
    return rows


def fit(
    candles_by_ticker: dict[str, list[Candle]],
    horizon_days: int,
    folds: int,
    seed: int,
) -> FittedModel:
    """Train a buy/no-buy classifier with walk-forward validation."""
    cfg = load()
    rows = _samples(
        candles_by_ticker, horizon_days, cfg.stop_loss_pct, cfg.take_profit_pct
    )
    if not rows:
        raise ValueError("not enough candles to train")
    features = [row[1] for row in rows]
    labels = [row[2] for row in rows]
    x = np.array(features, dtype=np.float64)
    y = np.array(labels, dtype=np.int32)
    n = len(rows)
    scores: list[Decimal] = []
    for fold in range(folds):
        train_end = n * (fold + 1) // (folds + 1)
        test_end = n * (fold + 2) // (folds + 1)
        if fold == folds - 1:
            test_end = n
        x_train, y_train = x[:train_end], y[:train_end]
        x_test, y_test = x[train_end:test_end], y[train_end:test_end]
        if len(y_train) == 0 or len(y_test) == 0:
            scores.append(Decimal("0"))
            continue
        if len(set(y_train.tolist())) < 2:
            scores.append(Decimal("0"))
            continue
        fold_model = LogisticRegression(random_state=seed, max_iter=1000)
        fold_model.fit(x_train, y_train)
        scores.append(Decimal(str(fold_model.score(x_test, y_test))))
    estimator = LogisticRegression(random_state=seed, max_iter=1000)
    estimator.fit(x, y)
    return FittedModel(
        estimator=estimator,
        features=FEATURE_NAMES,
        seed=seed,
        trained_at=now(),
        fold_scores=tuple(scores),
    )


def export(model: FittedModel, path: Path) -> Path:
    """Write a joblib bundle loadable by strategies.ml_model.load."""
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model.estimator,
            "features": list(model.features),
            "seed": model.seed,
            "trained_at": model.trained_at,
        },
        path,
    )
    return path
