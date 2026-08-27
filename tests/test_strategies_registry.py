"""Tests for zarabot.strategies.registry — written from technical-spec.md §3.2."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import joblib  # type: ignore[import-untyped]
import pytest

from zarabot.config import Config, ConfigError
from zarabot.strategies.ml_model import (
    FEATURE_NAMES,
    ModelContractError,
    ModelLoadError,
)
from zarabot.strategies.registry import enabled


class _ProbaModel:
    def __init__(self, buy_proba: float) -> None:
        self.buy_proba = buy_proba

    def predict_proba(self, x: object) -> list[list[float]]:
        n = len(x)  # type: ignore[arg-type]
        p = self.buy_proba
        return [[1.0 - p, p] for _ in range(n)]


def _config(
    enabled_strategies: tuple[str, ...] = (
        "ma_crossover",
        "rsi_reversion",
        "momentum",
    ),
    ml_model_path: Path | None = None,
) -> Config:
    return Config(
        tinvest_token="t",  # noqa: S106
        tinvest_account_id="a",
        trading_mode="live",
        telegram_bot_token="tg",  # noqa: S106
        telegram_chat_id=1,
        allocated_capital=Decimal("100000"),
        position_size_pct=Decimal("10"),
        stop_loss_pct=Decimal("5"),
        take_profit_pct=Decimal("10"),
        max_holding_days=3,
        max_open_positions=10,
        reentry_cooldown_minutes=120,
        daily_loss_limit_pct=Decimal("5"),
        watchlist=("SBER",),
        enabled_strategies=enabled_strategies,
        ml_model_path=ml_model_path,
        poll_interval_seconds=60,
        db_path=Path("zarabot.db"),
        backup_dir=Path("backups"),
        log_level="INFO",
        tz="Europe/Moscow",
    )


def _dump_model(path: Path, features: tuple[str, ...] | None = None) -> Path:
    joblib.dump(
        {
            "model": _ProbaModel(0.95),
            "features": list(features if features is not None else FEATURE_NAMES),
        },
        path,
    )
    return path


def test_enabled_returns_configured_rule_based_strategies() -> None:
    strategies = enabled(_config())
    assert [s.name for s in strategies] == [
        "ma_crossover",
        "rsi_reversion",
        "momentum",
    ]


def test_unknown_strategy_name_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="ENABLED_STRATEGIES"):
        enabled(_config(enabled_strategies=("not_a_strategy",)))


def test_ml_model_absent_when_path_unset() -> None:
    strategies = enabled(
        _config(
            enabled_strategies=(
                "ma_crossover",
                "ml_model",
                "momentum",
            ),
            ml_model_path=None,
        )
    )
    assert [s.name for s in strategies] == ["ma_crossover", "momentum"]


def test_ml_model_present_when_path_set(tmp_path: Path) -> None:
    path = _dump_model(tmp_path / "model.joblib")
    strategies = enabled(
        _config(
            enabled_strategies=("ma_crossover", "ml_model"),
            ml_model_path=path,
        )
    )
    assert [s.name for s in strategies] == ["ma_crossover", "ml_model"]


def test_missing_model_file_raises_model_load_error(tmp_path: Path) -> None:
    with pytest.raises(ModelLoadError):
        enabled(
            _config(
                enabled_strategies=("ml_model",),
                ml_model_path=tmp_path / "absent.joblib",
            )
        )


def test_mismatched_feature_manifest_raises_model_contract_error(
    tmp_path: Path,
) -> None:
    path = _dump_model(tmp_path / "stale.joblib", features=("wrong",))
    with pytest.raises(ModelContractError):
        enabled(
            _config(
                enabled_strategies=("ml_model",),
                ml_model_path=path,
            )
        )
