"""Structured logging with secret redaction. Writes JSON to stdout only."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from zarabot.clock import to_moscow

MASK = "***"
_MAX_DEPTH = 10

_RESERVED = frozenset(logging.makeLogRecord({"name": "x", "levelno": 0}).__dict__)


def _contains_secret(value: object, secrets: list[str]) -> bool:
    if not secrets:
        return False
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(_contains_secret(item, secrets) for item in value.values()) or any(
            _contains_secret(key, secrets) for key in value
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secrets) for item in value)
    if value is None or isinstance(value, (int, float, bool)):
        return False
    return _contains_secret(str(value), secrets)


def _redact_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def _redact_value(value: Any, secrets: list[str], depth: int) -> Any:
    if depth <= 0:
        return MASK
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, dict):
        return {
            (
                _redact_text(key, secrets) if isinstance(key, str) else key
            ): _redact_value(item, secrets, depth - 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secrets, depth - 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets, depth - 1) for item in value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    rendered = str(value)
    if _contains_secret(rendered, secrets):
        return _redact_text(rendered, secrets)
    return value


class _RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _RESERVED
        }
        message = record.getMessage()
        exc_text = record.exc_text
        if record.exc_info and not exc_text:
            exc_text = logging.Formatter().formatException(record.exc_info)
        if not (
            _contains_secret(message, self._secrets)
            or _contains_secret(extras, self._secrets)
            or _contains_secret(exc_text, self._secrets)
        ):
            return True
        record.msg = _redact_text(message, self._secrets)
        record.args = ()
        if extras:
            for key, value in extras.items():
                record.__dict__[key] = _redact_value(value, self._secrets, _MAX_DEPTH)
        if exc_text:
            record.exc_text = _redact_text(exc_text, self._secrets)
            record.exc_info = None
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "timestamp": created.isoformat(),
            "moscow_time": to_moscow(created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure(level: str, secrets: list[str]) -> None:
    """Install a JSON formatter on stdout and a secret-redaction filter."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RedactFilter(secrets))
    root.addHandler(handler)
