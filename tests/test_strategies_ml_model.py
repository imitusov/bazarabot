"""Tests for zarabot.strategies.ml_model — written from technical-spec.md §3.2."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import joblib  # type: ignore[import-untyped]
import pytest

from zarabot.models import Candle, Side
from zarabot.strategies.ml_model import (
    CONFIDENCE_THRESHOLD,
    FEATURE_NAMES,
    ModelContractError,
    ModelLoadError,
    build_features,
    load,
)

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)


class _ProbaModel:
    def __init__(self, buy_proba: float) -> None:
        self.buy_proba = buy_proba

    def predict_proba(self, x: object) -> list[list[float]]:
        n = len(x)  # type: ignore[arg-type]
        p = self.buy_proba
        return [[1.0 - p, p] for _ in range(n)]


def _candles(closes: Sequence[int | str]) -> list[Candle]:
    start = NOW - timedelta(days=len(closes))
    out: list[Candle] = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        out.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            )
        )
    return out


def _dump(
    path: Path,
    buy_proba: float,
    features: tuple[str, ...] | None = None,
) -> Path:
    joblib.dump(
        {
            "model": _ProbaModel(buy_proba),
            "features": list(features if features is not None else FEATURE_NAMES),
        },
        path,
    )
    return path


def _rising() -> list[Candle]:
    return _candles(list(range(100, 121)))


def test_entry_series_returns_signal(tmp_path: Path) -> None:
    strategy = load(_dump(tmp_path / "ok.joblib", 0.95))
    signal = strategy.evaluate("SBER", _rising(), NOW)
    assert signal is not None
    assert signal.ticker == "SBER"
    assert signal.strategy == "ml_model"
    assert signal.side is Side.BUY


def test_no_setup_returns_none(tmp_path: Path) -> None:
    strategy = load(_dump(tmp_path / "low.joblib", 0.1))
    assert strategy.evaluate("SBER", _rising(), NOW) is None


def test_short_series_returns_none(tmp_path: Path) -> None:
    strategy = load(_dump(tmp_path / "ok.joblib", 0.95))
    assert strategy.evaluate("SBER", _candles([100, 101, 102]), NOW) is None


def test_flat_series_returns_none(tmp_path: Path) -> None:
    strategy = load(_dump(tmp_path / "ok.joblib", 0.95))
    assert strategy.evaluate("SBER", _candles([100] * 30), NOW) is None


def test_evaluate_is_deterministic(tmp_path: Path) -> None:
    strategy = load(_dump(tmp_path / "ok.joblib", 0.95))
    candles = _rising()
    assert strategy.evaluate("SBER", candles, NOW) == strategy.evaluate(
        "SBER", candles, NOW
    )


def test_never_returns_sell(tmp_path: Path) -> None:
    high = load(_dump(tmp_path / "high.joblib", 0.95))
    low = load(_dump(tmp_path / "low.joblib", 0.1))
    for strategy, series in (
        (high, _rising()),
        (low, _rising()),
        (high, _candles([100] * 30)),
        (high, _candles([100])),
    ):
        signal = strategy.evaluate("SBER", series, NOW)
        if signal is not None:
            assert signal.side is not Side.SELL


def test_missing_model_file_raises_model_load_error(tmp_path: Path) -> None:
    with pytest.raises(ModelLoadError):
        load(tmp_path / "absent.joblib")


def test_unreadable_model_file_raises_model_load_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.joblib"
    path.write_bytes(b"not-a-joblib")
    with pytest.raises(ModelLoadError):
        load(path)


def test_mismatched_feature_manifest_raises_model_contract_error(
    tmp_path: Path,
) -> None:
    path = _dump(tmp_path / "stale.joblib", 0.95, features=("wrong_a", "wrong_b"))
    with pytest.raises(ModelContractError):
        load(path)


def test_prediction_below_confidence_threshold_returns_none(tmp_path: Path) -> None:
    below = float(CONFIDENCE_THRESHOLD) - 0.01
    strategy = load(_dump(tmp_path / "below.joblib", below))
    assert strategy.evaluate("SBER", _rising(), NOW) is None


def test_build_features_matches_feature_names_order() -> None:
    candles = _rising()
    values = build_features(candles)
    assert len(values) == len(FEATURE_NAMES)
    assert all(isinstance(value, float) for value in values)
    closes = [candle.close for candle in candles]
    last = candles[-1]
    expected = [
        float((closes[-1] - closes[-2]) / closes[-2]),
        float((closes[-1] - closes[-6]) / closes[-6]),
        float((last.high - last.low) / last.close),
        float(closes[-1] / (sum(closes[-10:], Decimal(0)) / Decimal(10))),
    ]
    assert values == expected


def test_build_features_rejects_short_series() -> None:
    with pytest.raises(ValueError):
        build_features(_candles([100, 101, 102]))
