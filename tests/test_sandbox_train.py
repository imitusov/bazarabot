"""Tests for sandbox.train — derived from the sandbox train contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sandbox.train import export, fit

from zarabot.models import Candle
from zarabot.strategies.ml_model import FEATURE_NAMES, load

NOW = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
REQUIRED_ENV = {
    "TINVEST_TOKEN": "token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "tg",
    "TELEGRAM_CHAT_ID": "1",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


def _candles(count: int, *, up: bool) -> list[Candle]:
    start = datetime(2025, 1, 1, 15, 0, tzinfo=UTC)
    out: list[Candle] = []
    price = Decimal("100")
    for i in range(count):
        if up:
            price = Decimal("100") + Decimal(i) * Decimal("0.8")
        else:
            price = Decimal("100") - Decimal(i) * Decimal("0.4")
        high = price * Decimal("1.02")
        low = price * Decimal("0.99")
        out.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=price,
                high=high,
                low=low,
                close=price,
                volume=1000,
            )
        )
    return out


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sandbox.train.now", lambda: NOW)


def test_fit_returns_seed_and_per_fold_scores(env: None) -> None:
    model = fit(
        {"SBER": _candles(80, up=True), "GAZP": _candles(80, up=False)},
        horizon_days=15,
        folds=3,
        seed=7,
    )
    assert model.seed == 7
    assert model.features == FEATURE_NAMES
    assert model.trained_at == NOW
    assert len(model.fold_scores) == 3


def test_export_is_loadable_by_ml_model(env: None, tmp_path: Path) -> None:
    fitted = fit({"SBER": _candles(80, up=True)}, 15, 3, 3)
    path = export(fitted, tmp_path / "model.joblib")
    loaded = load(path)
    assert loaded.estimator is not None
    candles = _candles(20, up=True)
    signal = loaded.evaluate("SBER", candles, NOW)
    assert signal is None or signal.ticker == "SBER"


def test_walk_forward_uses_expanding_train(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []

    class _Spy:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def fit(self, x: object, y: object) -> _Spy:
            sizes.append(len(y))  # type: ignore[arg-type]
            return self

        def predict(self, x: object) -> list[int]:
            return [0] * len(x)  # type: ignore[arg-type]

        def score(self, x: object, y: object) -> float:
            return 0.5

        def predict_proba(self, x: object) -> list[list[float]]:
            n = len(x)  # type: ignore[arg-type]
            return [[0.7, 0.3] for _ in range(n)]

    monkeypatch.setattr("sandbox.train.LogisticRegression", _Spy)
    fit({"SBER": _candles(80, up=True)}, 15, 3, 1)
    assert len(sizes) == 3
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_fit_imports_build_features(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = __import__(
        "zarabot.strategies.ml_model", fromlist=["build_features"]
    ).build_features

    def wrapped(candles: list[Candle]) -> list[float]:
        calls["n"] += 1
        return real(candles)

    monkeypatch.setattr("sandbox.train.build_features", wrapped)
    fit({"SBER": _candles(40, up=True)}, 10, 2, 1)
    assert calls["n"] > 0
