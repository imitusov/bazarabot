"""Loads and validates every setting once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

_DEFAULTS: dict[str, str] = {
    "TRADING_MODE": "live",
    "POSITION_SIZE_PCT": "10",
    "MAX_POSITION_PCT": "20",
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
        "MAX_POSITION_PCT",
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
    max_position_pct: Decimal
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
            f"max_position_pct={self.max_position_pct!r}, "
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
            f"ssl_tbank_verify={self.ssl_tbank_verify!r})"
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

    position_size = _pct("POSITION_SIZE_PCT", _optional("POSITION_SIZE_PCT"))
    max_position = _pct("MAX_POSITION_PCT", _optional("MAX_POSITION_PCT"))
    if position_size > max_position:
        raise ConfigError("POSITION_SIZE_PCT exceeds MAX_POSITION_PCT")

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

    return Config(
        tinvest_token=token,
        tinvest_account_id=account_id,
        trading_mode=trading_mode,
        telegram_bot_token=telegram_token,
        telegram_chat_id=_int("TELEGRAM_CHAT_ID", _require("TELEGRAM_CHAT_ID")),
        allocated_capital=allocated,
        position_size_pct=position_size,
        max_position_pct=max_position,
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
    )
