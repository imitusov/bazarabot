"""Tests for zarabot.config — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.config import ConfigError, load

REQUIRED = {
    "TINVEST_TOKEN": "tinvest-secret-token",
    "TINVEST_ACCOUNT_ID": "account-id",
    "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
    "TELEGRAM_CHAT_ID": "123456789",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER,GAZP,LKOH",
}


def _env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str] | None = None) -> None:
    for key in (
        "TINVEST_TOKEN",
        "TINVEST_ACCOUNT_ID",
        "TINVEST_TOKEN_SANDBOX",
        "TINVEST_ACCOUNT_ID_SANDBOX",
        "TRADING_MODE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ALLOCATED_CAPITAL",
        "POSITION_SIZE_PCT",
        "MAX_POSITION_PCT",
        "STOP_LOSS_PCT",
        "TAKE_PROFIT_PCT",
        "MAX_HOLDING_DAYS",
        "MAX_OPEN_POSITIONS",
        "REENTRY_COOLDOWN_MINUTES",
        "DAILY_LOSS_LIMIT_PCT",
        "WATCHLIST",
        "ENABLED_STRATEGIES",
        "ML_MODEL_PATH",
        "POLL_INTERVAL_SECONDS",
        "DB_PATH",
        "BACKUP_DIR",
        "LOG_LEVEL",
        "TZ",
        "SSL_TBANK_VERIFY",
        "PRICE_MAX_AGE_SECONDS",
        "PRICE_MAX_MOVE_PCT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    if extra:
        for key, value in extra.items():
            monkeypatch.setenv(key, value)


def test_complete_environment_produces_populated_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    cfg = load()
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]
    assert cfg.watchlist == ("SBER", "GAZP", "LKOH")
    assert cfg.position_size_pct == Decimal("10")
    assert cfg.trading_mode == "live"
    assert cfg.ssl_tbank_verify is True
    assert cfg.price_max_age_seconds == 120
    assert cfg.price_max_move_pct == Decimal("20")


def test_missing_tinvest_token_raises_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("TINVEST_TOKEN")
    with pytest.raises(ConfigError, match="TINVEST_TOKEN") as exc:
        load()
    assert "tinvest-secret-token" not in str(exc.value)


def test_position_size_pct_zero_or_above_100_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"POSITION_SIZE_PCT": "0"})
    with pytest.raises(ConfigError, match="POSITION_SIZE_PCT"):
        load()
    _env(monkeypatch, {"POSITION_SIZE_PCT": "101"})
    with pytest.raises(ConfigError, match="POSITION_SIZE_PCT"):
        load()


def test_position_size_pct_above_max_position_pct_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"POSITION_SIZE_PCT": "15", "MAX_POSITION_PCT": "10"})
    with pytest.raises(ConfigError, match="POSITION_SIZE_PCT"):
        load()


def test_max_open_positions_times_size_exceeding_100_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(
        monkeypatch,
        {
            "POSITION_SIZE_PCT": "20",
            "MAX_POSITION_PCT": "20",
            "MAX_OPEN_POSITIONS": "6",
        },
    )
    with pytest.raises(ConfigError, match="MAX_OPEN_POSITIONS"):
        load()


def test_take_profit_not_greater_than_stop_loss_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"STOP_LOSS_PCT": "10", "TAKE_PROFIT_PCT": "10"})
    with pytest.raises(ConfigError, match="TAKE_PROFIT_PCT"):
        load()
    _env(monkeypatch, {"STOP_LOSS_PCT": "10", "TAKE_PROFIT_PCT": "5"})
    with pytest.raises(ConfigError, match="TAKE_PROFIT_PCT"):
        load()


def test_empty_watchlist_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {"WATCHLIST": ""})
    with pytest.raises(ConfigError, match="WATCHLIST"):
        load()
    _env(monkeypatch, {"WATCHLIST": "  ,  "})
    with pytest.raises(ConfigError, match="WATCHLIST"):
        load()


def test_config_string_form_contains_neither_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    cfg = load()
    text = str(cfg) + repr(cfg)
    assert "tinvest-secret-token" not in text
    assert "telegram-secret-token" not in text


def test_ssl_tbank_verify_defaults_true_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    assert load().ssl_tbank_verify is True


def test_ssl_tbank_verify_true_and_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "true"})
    assert load().ssl_tbank_verify is True
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "false"})
    assert load().ssl_tbank_verify is False


def test_ssl_tbank_verify_false_logs_critical_naming_the_risk(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "false"})
    with caplog.at_level(logging.CRITICAL):
        load()
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical
    joined = " ".join(r.getMessage() for r in critical).lower()
    assert "certificate" in joined
    assert "token" in joined
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text


def test_ssl_tbank_verify_true_does_not_log_critical(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "true"})
    with caplog.at_level(logging.CRITICAL):
        load()
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_price_max_age_seconds_defaults_to_120(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    assert load().price_max_age_seconds == 120


def test_price_max_age_seconds_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"PRICE_MAX_AGE_SECONDS": "30"})
    assert load().price_max_age_seconds == 30


def test_price_max_age_seconds_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"PRICE_MAX_AGE_SECONDS": "0"})
    with pytest.raises(ConfigError, match="PRICE_MAX_AGE_SECONDS"):
        load()


def test_price_max_move_pct_defaults_to_20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    assert load().price_max_move_pct == Decimal("20")


def test_price_max_move_pct_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"PRICE_MAX_MOVE_PCT": "15"})
    assert load().price_max_move_pct == Decimal("15")


def test_price_max_move_pct_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"PRICE_MAX_MOVE_PCT": "0"})
    with pytest.raises(ConfigError, match="PRICE_MAX_MOVE_PCT"):
        load()
    _env(monkeypatch, {"PRICE_MAX_MOVE_PCT": "101"})
    with pytest.raises(ConfigError, match="PRICE_MAX_MOVE_PCT"):
        load()


def test_ssl_tbank_verify_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "maybe"})
    with pytest.raises(ConfigError, match="SSL_TBANK_VERIFY"):
        load()


def test_unreadable_ml_model_path_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "absent" / "model.joblib"
    _env(monkeypatch, {"ML_MODEL_PATH": str(missing)})
    with pytest.raises(ConfigError, match="ML_MODEL_PATH"):
        load()


SANDBOX = {
    "TINVEST_TOKEN_SANDBOX": "sandbox-secret-token",
    "TINVEST_ACCOUNT_ID_SANDBOX": "sandbox-account-id",
}


def test_sandbox_mode_resolves_the_sandbox_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"TRADING_MODE": "sandbox", **SANDBOX})
    cfg = load()
    assert cfg.trading_mode == "sandbox"
    assert cfg.tinvest_token == SANDBOX["TINVEST_TOKEN_SANDBOX"]
    assert cfg.tinvest_account_id == SANDBOX["TINVEST_ACCOUNT_ID_SANDBOX"]


def test_live_mode_ignores_the_sandbox_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"TRADING_MODE": "live", **SANDBOX})
    cfg = load()
    assert cfg.tinvest_token == REQUIRED["TINVEST_TOKEN"]
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]


def test_sandbox_mode_falls_back_to_the_base_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"TRADING_MODE": "sandbox"})
    cfg = load()
    assert cfg.tinvest_token == REQUIRED["TINVEST_TOKEN"]
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]


def test_empty_sandbox_override_falls_back_rather_than_authenticating_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(
        monkeypatch,
        {
            "TRADING_MODE": "sandbox",
            "TINVEST_TOKEN_SANDBOX": "   ",
            "TINVEST_ACCOUNT_ID_SANDBOX": "",
        },
    )
    cfg = load()
    assert cfg.tinvest_token == REQUIRED["TINVEST_TOKEN"]
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]


def test_sandbox_token_is_overridden_independently_of_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(
        monkeypatch,
        {
            "TRADING_MODE": "sandbox",
            "TINVEST_TOKEN_SANDBOX": SANDBOX["TINVEST_TOKEN_SANDBOX"],
        },
    )
    cfg = load()
    assert cfg.tinvest_token == SANDBOX["TINVEST_TOKEN_SANDBOX"]
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]


def test_config_string_form_hides_the_sandbox_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"TRADING_MODE": "sandbox", **SANDBOX})
    cfg = load()
    text = str(cfg) + repr(cfg)
    assert SANDBOX["TINVEST_TOKEN_SANDBOX"] not in text
