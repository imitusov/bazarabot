"""Tests for zarabot.config — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from zarabot.config import ConfigError, get, load

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_config_memo() -> Iterator[None]:
    """Drop the process-wide memo around every test in this file.

    `get()` caches for the life of the process. Without this, whichever
    environment the first test happened to set would be inherited by every
    later test — here and in every file that runs after this one.
    """
    get.cache_clear()
    yield
    get.cache_clear()


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
        "CASH_RESERVE_PCT",
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
        "ALLOW_FOREIGN_HOLDINGS",
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
    assert cfg.cash_reserve_pct == Decimal("1")


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


def test_max_position_pct_is_withdrawn_and_no_longer_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # v1.30 withdrew MAX_POSITION_PCT (#15). The cross-field check that used to
    # live here guaranteed position_size_pct <= max_position_pct, which is
    # exactly what made the per-position cap unreachable in sizing while
    # /resume reported it as an active limit. The variable is now ignored
    # entirely: a value that would once have been rejected must load, and the
    # field must be gone from Config rather than kept and unused — an
    # unenforced limit still on the object is one a later reader will display.
    _env(monkeypatch, {"POSITION_SIZE_PCT": "15", "MAX_POSITION_PCT": "10"})
    cfg = load()
    assert cfg.position_size_pct == Decimal("15")
    assert not hasattr(cfg, "max_position_pct")
    assert "max_position_pct" not in str(cfg) + repr(cfg)


def test_max_open_positions_times_size_exceeding_100_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one cross-field bound that survives v1.30: the allocation cannot be
    # structurally over-committed. Unlike the per-position cap, this one can
    # bind, so it stays a configuration-time refusal.
    _env(
        monkeypatch,
        {
            "POSITION_SIZE_PCT": "20",
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


def test_cash_reserve_pct_defaults_to_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert load().cash_reserve_pct == Decimal("1")


def test_cash_reserve_pct_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {"CASH_RESERVE_PCT": "2.5"})
    assert load().cash_reserve_pct == Decimal("2.5")


def test_cash_reserve_pct_accepts_the_inclusive_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0 is a legitimate choice — hold nothing back — and 50 is the documented
    # ceiling. Both bounds are inclusive, unlike every other percentage here,
    # which is why this one cannot go through the ordinary 0 < x <= 100 rule.
    _env(monkeypatch, {"CASH_RESERVE_PCT": "0"})
    assert load().cash_reserve_pct == Decimal("0")
    _env(monkeypatch, {"CASH_RESERVE_PCT": "50"})
    assert load().cash_reserve_pct == Decimal("50")


@pytest.mark.parametrize("raw", ["-1", "50.01", "51", "100"])
def test_cash_reserve_pct_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # A reserve above half of cash is a configuration error, not a preference:
    # it would starve sizing of the cash the operator meant it to deploy.
    _env(monkeypatch, {"CASH_RESERVE_PCT": raw})
    with pytest.raises(ConfigError, match="CASH_RESERVE_PCT"):
        load()


def test_cash_reserve_pct_not_a_number_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {"CASH_RESERVE_PCT": "one"})
    with pytest.raises(ConfigError, match="CASH_RESERVE_PCT"):
        load()


def test_cash_reserve_pct_is_a_decimal_never_a_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It scales cash before sizing divides by a lot cost; a float here would
    # put binary rounding on the affordability arithmetic.
    _env(monkeypatch, {"CASH_RESERVE_PCT": "1"})
    assert isinstance(load().cash_reserve_pct, Decimal)


def test_cash_reserve_pct_appears_in_the_string_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"CASH_RESERVE_PCT": "3"})
    text = str(load()) + repr(load())
    assert "cash_reserve_pct" in text


def test_ssl_tbank_verify_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "maybe"})
    with pytest.raises(ConfigError, match="SSL_TBANK_VERIFY"):
        load()


def test_allow_foreign_holdings_defaults_to_false_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    assert load().allow_foreign_holdings is False


def test_allow_foreign_holdings_blank_takes_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"ALLOW_FOREIGN_HOLDINGS": "   "})
    assert load().allow_foreign_holdings is False


def test_allow_foreign_holdings_true_and_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"ALLOW_FOREIGN_HOLDINGS": "true"})
    assert load().allow_foreign_holdings is True
    _env(monkeypatch, {"ALLOW_FOREIGN_HOLDINGS": "false"})
    assert load().allow_foreign_holdings is False


@pytest.mark.parametrize("raw", ["1", "yes", "TRUE", "True", "on", "maybe"])
def test_allow_foreign_holdings_invalid_raises_rather_than_enabling(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # The flag must be awkward to set by accident: no near-miss spelling may
    # quietly acknowledge foreign holdings, and none may quietly be ignored.
    _env(monkeypatch, {"ALLOW_FOREIGN_HOLDINGS": raw})
    with pytest.raises(ConfigError, match="ALLOW_FOREIGN_HOLDINGS"):
        load()


def test_allow_foreign_holdings_appears_in_the_string_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"ALLOW_FOREIGN_HOLDINGS": "true"})
    cfg = load()
    text = str(cfg) + repr(cfg)
    assert "allow_foreign_holdings=True" in text


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


def test_get_returns_a_populated_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = get()
    assert cfg.tinvest_account_id == REQUIRED["TINVEST_ACCOUNT_ID"]
    assert cfg.watchlist == ("SBER", "GAZP", "LKOH")
    assert cfg.allocated_capital == Decimal("100000")


def test_get_returns_the_same_instance_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    assert get() is get() is get()


def test_get_does_not_reread_the_environment_after_the_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The point of the memo (#18): a per-order read on the latency-critical
    # path must not re-parse every variable. A later environment change is
    # therefore invisible to `get()` until the memo is cleared.
    _env(monkeypatch, {"POSITION_SIZE_PCT": "5"})
    first = get()
    assert first.position_size_pct == Decimal("5")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")
    assert get() is first
    assert get().position_size_pct == Decimal("5")


def test_load_still_rereads_the_environment_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `load()` is unchanged: `app.startup` calls it so a bad configuration
    # fails before anything else, and it must see the environment as it is.
    _env(monkeypatch, {"POSITION_SIZE_PCT": "5"})
    assert load().position_size_pct == Decimal("5")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")
    assert load().position_size_pct == Decimal("10")
    first = load()
    assert load() is not first


def test_get_reflects_the_environment_after_the_memo_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, {"POSITION_SIZE_PCT": "5"})
    assert get().position_size_pct == Decimal("5")
    monkeypatch.setenv("POSITION_SIZE_PCT", "10")
    get.cache_clear()
    assert get().position_size_pct == Decimal("10")


def test_get_raises_config_error_on_an_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("TINVEST_TOKEN")
    with pytest.raises(ConfigError, match="TINVEST_TOKEN") as exc:
        get()
    assert "tinvest-secret-token" not in str(exc.value)


def test_get_does_not_memoise_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed load must leave nothing behind: a process that fixed its
    # environment and retried would otherwise keep failing on a cached error,
    # and — worse — a cached *success* must never be produced from a
    # half-validated load.
    _env(monkeypatch)
    monkeypatch.delenv("TINVEST_TOKEN")
    with pytest.raises(ConfigError):
        get()
    monkeypatch.setenv("TINVEST_TOKEN", REQUIRED["TINVEST_TOKEN"])
    assert get().tinvest_token == REQUIRED["TINVEST_TOKEN"]


def test_get_string_form_contains_neither_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    text = str(get()) + repr(get())
    assert "tinvest-secret-token" not in text
    assert "telegram-secret-token" not in text


def test_importing_the_module_loads_nothing(tmp_path: Path) -> None:
    # `AGENTS.md` allows module-level side effects in `config`, but the memo
    # must still fill lazily: importing with no environment at all must not
    # raise, and must not leave a config behind.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TINVEST_", "TELEGRAM_", "ALLOCATED_", "WATCHLIST"))
    }
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from zarabot.config import ConfigError, get\n"
            "assert get.cache_info().currsize == 0\n"
            "raised = False\n"
            "try:\n"
            "    get()\n"
            "except ConfigError:\n"
            "    raised = True\n"
            "assert raised\n"
            "assert get.cache_info().currsize == 0\n",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_get_logs_the_ssl_warning_only_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The CRITICAL line belongs to loading, not to reading: memoised reads
    # must not turn one disabled-verification warning into a flood that
    # buries it.
    _env(monkeypatch, {"SSL_TBANK_VERIFY": "false"})
    with caplog.at_level(logging.CRITICAL):
        get()
        get()
        get()
    assert len([r for r in caplog.records if r.levelno == logging.CRITICAL]) == 1
