"""Loads and validates every setting once at startup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

_DEFAULTS: dict[str, str] = {
    "TRADING_MODE": "live",
    "POSITION_SIZE_PCT": "10",
    "CASH_RESERVE_PCT": "1",
    "STOP_LOSS_PCT": "5",
    "TAKE_PROFIT_PCT": "10",
    "MAX_HOLDING_DAYS": "3",
    "MAX_OPEN_POSITIONS": "10",
    "REENTRY_COOLDOWN_MINUTES": "120",
    "DAILY_LOSS_LIMIT_PCT": "5",
    "ENABLED_STRATEGIES": "ma_crossover,rsi_reversion,momentum",
    "POLL_INTERVAL_SECONDS": "60",
    "DB_PATH": "/data/zarabot.db",
    "BACKUP_DIR": "/data/backups",
    "LOG_LEVEL": "INFO",
    "TZ": "Europe/Moscow",
    "SSL_TBANK_VERIFY": "true",
    "PRICE_MAX_AGE_SECONDS": "120",
    "PRICE_MAX_MOVE_PCT": "20",
    "ALLOW_FOREIGN_HOLDINGS": "false",
}

_REQUIRED = (
    "TINVEST_TOKEN",
    "TINVEST_ACCOUNT_ID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALLOCATED_CAPITAL",
    "WATCHLIST",
)

_RISK_VARS = frozenset(
    {
        "ALLOCATED_CAPITAL",
        "POSITION_SIZE_PCT",
        "STOP_LOSS_PCT",
        "TAKE_PROFIT_PCT",
        "MAX_HOLDING_DAYS",
        "MAX_OPEN_POSITIONS",
        "REENTRY_COOLDOWN_MINUTES",
        "DAILY_LOSS_LIMIT_PCT",
    }
)

_REDACT = "***"


class ConfigError(Exception):
    """Invalid or missing configuration. Message names the variable, never a token."""


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _require(name: str) -> str:
    value = _raw(name)
    if value is None:
        raise ConfigError(f"{name} is missing or empty")
    return value


def _optional(name: str) -> str:
    value = _raw(name)
    if value is not None:
        return value
    if name in _RISK_VARS and name not in _DEFAULTS:
        raise ConfigError(f"{name} is missing or empty")
    return _DEFAULTS[name]


def _decimal(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} is not a valid number") from exc
    return value


def _int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid integer") from exc


def _pct(name: str, raw: str) -> Decimal:
    value = _decimal(name, raw)
    if value <= 0 or value > 100:
        raise ConfigError(f"{name} is out of range")
    return value


def _pct_inclusive(name: str, raw: str, low: Decimal, high: Decimal) -> Decimal:
    """A percentage whose bounds are both inclusive, unlike `_pct`."""
    value = _decimal(name, raw)
    if value < low or value > high:
        raise ConfigError(f"{name} is out of range")
    return value


def _positive_int(name: str, raw: str) -> int:
    value = _int(name, raw)
    if value <= 0:
        raise ConfigError(f"{name} is out of range")
    return value


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _bool(name: str, raw: str) -> bool:
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigError(f"{name} is out of range")


@dataclass(frozen=True)
class Config:
    tinvest_token: str
    tinvest_account_id: str
    trading_mode: str
    telegram_bot_token: str
    telegram_chat_id: int
    allocated_capital: Decimal
    position_size_pct: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    max_holding_days: int
    max_open_positions: int
    reentry_cooldown_minutes: int
    daily_loss_limit_pct: Decimal
    watchlist: tuple[str, ...]
    enabled_strategies: tuple[str, ...]
    ml_model_path: Path | None
    poll_interval_seconds: int
    db_path: Path
    backup_dir: Path
    log_level: str
    tz: str
    ssl_tbank_verify: bool = True
    price_max_age_seconds: int = 120
    price_max_move_pct: Decimal = Decimal("20")
    cash_reserve_pct: Decimal = Decimal("1")
    allow_foreign_holdings: bool = False

    def __repr__(self) -> str:
        return (
            "Config("
            f"tinvest_token={_REDACT!r}, "
            f"tinvest_account_id={self.tinvest_account_id!r}, "
            f"trading_mode={self.trading_mode!r}, "
            f"telegram_bot_token={_REDACT!r}, "
            f"telegram_chat_id={self.telegram_chat_id!r}, "
            f"allocated_capital={self.allocated_capital!r}, "
            f"position_size_pct={self.position_size_pct!r}, "
            f"stop_loss_pct={self.stop_loss_pct!r}, "
            f"take_profit_pct={self.take_profit_pct!r}, "
            f"max_holding_days={self.max_holding_days!r}, "
            f"max_open_positions={self.max_open_positions!r}, "
            f"reentry_cooldown_minutes={self.reentry_cooldown_minutes!r}, "
            f"daily_loss_limit_pct={self.daily_loss_limit_pct!r}, "
            f"watchlist={self.watchlist!r}, "
            f"enabled_strategies={self.enabled_strategies!r}, "
            f"ml_model_path={self.ml_model_path!r}, "
            f"poll_interval_seconds={self.poll_interval_seconds!r}, "
            f"db_path={self.db_path!r}, "
            f"backup_dir={self.backup_dir!r}, "
            f"log_level={self.log_level!r}, "
            f"tz={self.tz!r}, "
            f"ssl_tbank_verify={self.ssl_tbank_verify!r}, "
            f"price_max_age_seconds={self.price_max_age_seconds!r}, "
            f"price_max_move_pct={self.price_max_move_pct!r}, "
            f"cash_reserve_pct={self.cash_reserve_pct!r}, "
            f"allow_foreign_holdings={self.allow_foreign_holdings!r})"
        )

    __str__ = __repr__


def load() -> Config:
    for name in _REQUIRED:
        _require(name)

    token = _require("TINVEST_TOKEN")
    account_id = _require("TINVEST_ACCOUNT_ID")
    telegram_token = _require("TELEGRAM_BOT_TOKEN")
    trading_mode = _optional("TRADING_MODE")
    if trading_mode not in {"live", "sandbox"}:
        raise ConfigError("TRADING_MODE is out of range")

    # Sandbox is a separate broker environment, not a flag on the live one: its
    # accounts do not exist on the live endpoint and are usually reached with a
    # different token. Resolve the pair for the mode in force, so every other
    # module still sees exactly one token and one account id. Each override is
    # independent and falls back to the base value when unset, because the same
    # token often works against both endpoints while the account id never does.
    if trading_mode == "sandbox":
        token = _raw("TINVEST_TOKEN_SANDBOX") or token
        account_id = _raw("TINVEST_ACCOUNT_ID_SANDBOX") or account_id

    allocated = _decimal("ALLOCATED_CAPITAL", _require("ALLOCATED_CAPITAL"))
    if allocated <= 0:
        raise ConfigError("ALLOCATED_CAPITAL is out of range")

    # MAX_POSITION_PCT was withdrawn in v1.30 (#15) along with its cross-field
    # check against POSITION_SIZE_PCT. The check guaranteed
    # position_size_pct <= max_position_pct, so the per-position cap was never
    # the binding minimum in sizing and could reject nothing — while /resume
    # named it as an active control. A limit that cannot bind is worse than no
    # limit, because it is believed. The variable is now read by nothing.
    position_size = _pct("POSITION_SIZE_PCT", _optional("POSITION_SIZE_PCT"))

    stop_loss = _pct("STOP_LOSS_PCT", _optional("STOP_LOSS_PCT"))
    take_profit = _pct("TAKE_PROFIT_PCT", _optional("TAKE_PROFIT_PCT"))
    if take_profit <= stop_loss:
        raise ConfigError("TAKE_PROFIT_PCT must be greater than STOP_LOSS_PCT")

    max_open = _positive_int("MAX_OPEN_POSITIONS", _optional("MAX_OPEN_POSITIONS"))
    if max_open * position_size > 100:
        raise ConfigError("MAX_OPEN_POSITIONS × POSITION_SIZE_PCT exceeds 100")

    watchlist = _csv(_require("WATCHLIST"))
    if not watchlist:
        raise ConfigError("WATCHLIST is missing or empty")

    ml_raw = _raw("ML_MODEL_PATH")
    ml_path: Path | None
    if ml_raw:
        ml_path = Path(ml_raw)
        if not ml_path.is_file() or not os.access(ml_path, os.R_OK):
            raise ConfigError("ML_MODEL_PATH is set but unreadable")
    else:
        ml_path = None

    cfg = Config(
        tinvest_token=token,
        tinvest_account_id=account_id,
        trading_mode=trading_mode,
        telegram_bot_token=telegram_token,
        telegram_chat_id=_int("TELEGRAM_CHAT_ID", _require("TELEGRAM_CHAT_ID")),
        allocated_capital=allocated,
        position_size_pct=position_size,
        stop_loss_pct=stop_loss,
        take_profit_pct=take_profit,
        max_holding_days=_positive_int(
            "MAX_HOLDING_DAYS", _optional("MAX_HOLDING_DAYS")
        ),
        max_open_positions=max_open,
        reentry_cooldown_minutes=_positive_int(
            "REENTRY_COOLDOWN_MINUTES", _optional("REENTRY_COOLDOWN_MINUTES")
        ),
        daily_loss_limit_pct=_pct(
            "DAILY_LOSS_LIMIT_PCT", _optional("DAILY_LOSS_LIMIT_PCT")
        ),
        watchlist=watchlist,
        enabled_strategies=_csv(_optional("ENABLED_STRATEGIES")),
        ml_model_path=ml_path,
        poll_interval_seconds=_positive_int(
            "POLL_INTERVAL_SECONDS", _optional("POLL_INTERVAL_SECONDS")
        ),
        db_path=Path(_optional("DB_PATH")),
        backup_dir=Path(_optional("BACKUP_DIR")),
        log_level=_optional("LOG_LEVEL"),
        tz=_optional("TZ"),
        ssl_tbank_verify=_bool("SSL_TBANK_VERIFY", _optional("SSL_TBANK_VERIFY")),
        price_max_age_seconds=_positive_int(
            "PRICE_MAX_AGE_SECONDS", _optional("PRICE_MAX_AGE_SECONDS")
        ),
        price_max_move_pct=_pct("PRICE_MAX_MOVE_PCT", _optional("PRICE_MAX_MOVE_PCT")),
        # The slice of cash risk.sizing holds back so fees and rounding cannot
        # make an approved order unaffordable. Both bounds are inclusive: 0 is
        # a legitimate choice, and a reserve above half the cash is a
        # configuration error rather than a preference, because it would
        # starve sizing of the money the operator meant it to deploy.
        cash_reserve_pct=_pct_inclusive(
            "CASH_RESERVE_PCT",
            _optional("CASH_RESERVE_PCT"),
            Decimal("0"),
            Decimal("50"),
        ),
        # Not a risk limit, so a missing value takes the safe default. Anything
        # else must be spelled exactly: the flag says the trading account is not
        # the bot's alone, and nobody should arrive at that by writing "yes".
        allow_foreign_holdings=_bool(
            "ALLOW_FOREIGN_HOLDINGS", _optional("ALLOW_FOREIGN_HOLDINGS")
        ),
    )
    if not cfg.ssl_tbank_verify:
        _log.critical(
            "SSL_TBANK_VERIFY is false: certificate verification is disabled "
            "on the connection that carries the trading token"
        )
    return cfg


@lru_cache(maxsize=1)
def get() -> Config:
    """Return the process-wide `Config`, loading it on the first call only.

    `load()` re-reads every environment variable, re-parses every `Decimal` and
    stats `ML_MODEL_PATH` on each call; `broker.client` was paying that three
    times per order on the latency-critical path (#18). Every later reader uses
    this instead.

    The memo fills lazily rather than at import: `AGENTS.md` permits a
    module-level side effect here, but loading at import would make merely
    importing `config` — from a test, a script or a tool — validate whichever
    environment happened to be in force, and fail there rather than in
    `app.startup`, which calls `load()` first precisely so a bad configuration
    aborts before anything else.

    A failed load caches nothing: `lru_cache` records a result only when the
    call returns, so a `ConfigError` leaves the memo empty and the next call
    re-reads. Tests clear it with `get.cache_clear()`; nothing in the running
    bot ever should, because a config swapped mid-flight would change a risk
    limit under an open position.
    """
    return load()
