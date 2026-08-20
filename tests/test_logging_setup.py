"""Tests for zarabot.logging_setup — written from technical-spec.md §3.2."""

from __future__ import annotations

import json
import logging

import pytest

from zarabot.logging_setup import MASK, configure

TOKEN = "tinvest-secret-token-value"
LOGGER_NAME = "zarabot.test_logging"


def _logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def test_token_in_message_is_replaced_by_mask(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO", [TOKEN])
    _logger().info("authenticated with %s", TOKEN)
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert MASK in out


def test_token_in_structured_field_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO", [TOKEN])
    _logger().info("order accepted", extra={"broker_token": TOKEN, "order": "abc"})
    out = capsys.readouterr().out
    assert TOKEN not in out
    payload = json.loads(out.strip().splitlines()[-1])
    dumped = json.dumps(payload)
    assert TOKEN not in dumped
    assert MASK in dumped


def test_token_in_exception_traceback_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO", [TOKEN])
    try:
        raise RuntimeError(f"broker rejected token {TOKEN}")
    except RuntimeError:
        _logger().exception("submit failed")
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert MASK in captured.out


def test_record_containing_no_secret_passes_through_byte_identical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "heartbeat ok"
    configure("INFO", [TOKEN])
    _logger().info(message)
    redacting = capsys.readouterr().out
    configure("INFO", [])
    _logger().info(message)
    plain = capsys.readouterr().out
    redacting_body = json.loads(redacting.strip().splitlines()[-1])
    plain_body = json.loads(plain.strip().splitlines()[-1])
    redacting_body.pop("timestamp", None)
    plain_body.pop("timestamp", None)
    assert redacting_body == plain_body
    assert message.encode() in redacting.encode()
    assert message.encode() in plain.encode()
