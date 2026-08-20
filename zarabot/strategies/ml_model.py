"""Optional ML strategy. Load at startup; evaluate is pure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np

from zarabot.models import Candle, Side, Signal

FEATURE_NAMES: tuple[str, ...] = (
    "return_1",
    "return_5",
    "high_low_range",
    "close_sma_10",
)
CONFIDENCE_THRESHOLD = Decimal("0.60")
_SMA_PERIOD = 10
_LOOKBACK = 11


class ModelLoadError(Exception):
    """Model file missing or unreadable."""


class ModelContractError(Exception):
    """Feature manifest does not match the features this module builds."""


def _sma(closes: list[Decimal], period: int) -> Decimal:
    window = closes[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


def build_features(candles: list[Candle]) -> list[float]:
    """Feature vector in FEATURE_NAMES order. Raises if shorter than lookback."""
    if len(candles) < _LOOKBACK:
        raise ValueError("not enough candles to build features")
    closes = [c.close for c in candles]
    last = candles[-1]
    return_1 = (closes[-1] - closes[-2]) / closes[-2]
    return_5 = (closes[-1] - closes[-6]) / closes[-6]
    high_low_range = (last.high - last.low) / last.close
    close_sma_10 = closes[-1] / _sma(closes, _SMA_PERIOD)
    return [
        float(return_1),
        float(return_5),
        float(high_low_range),
        float(close_sma_10),
    ]


@dataclass(frozen=True)
class LoadedModel:
    estimator: Any
    name: str = "ml_model"
    lookback: int = _LOOKBACK

    def evaluate(
        self, ticker: str, candles: list[Candle], now: datetime
    ) -> Signal | None:
        if len(candles) < self.lookback:
            return None
        closes = [c.close for c in candles]
        if len(set(closes)) == 1:
            return None
        row = build_features(candles)
        x = np.array([row], dtype=np.float64)
        proba = self.estimator.predict_proba(x)[0]
        buy_p = Decimal(str(proba[1]))
        if buy_p < CONFIDENCE_THRESHOLD:
            return None
        return Signal(
            ticker=ticker,
            strategy=self.name,
            side=Side.BUY,
            generated_at=now,
            reference_price=closes[-1],
        )


def load(path: Path) -> LoadedModel:
    if not path.is_file():
        raise ModelLoadError(f"model file missing: {path}")
    try:
        payload = joblib.load(path)  # noqa: S301 — contracted joblib format
    except Exception as exc:
        raise ModelLoadError(f"model file unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ModelLoadError(f"model file unreadable: {path}")
    try:
        model = payload["model"]
        raw_features = payload["features"]
    except (KeyError, TypeError) as exc:
        raise ModelLoadError(f"model file unreadable: {path}") from exc
    if tuple(raw_features) != FEATURE_NAMES:
        raise ModelContractError(
            "feature manifest does not match expected names and order"
        )
    if not hasattr(model, "predict_proba"):
        raise ModelLoadError(f"model file unreadable: {path}")
    return LoadedModel(estimator=model)
