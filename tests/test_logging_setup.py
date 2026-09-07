"""Tests for zarabot.logging_setup — written from technical-spec.md §3.2."""

from __future__ import annotations

import json
import logging

import pytest

from zarabot.logging_setup import MASK, configure

TOKEN = "tinvest-secret-token-value"  # noqa: S105
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
    _logger().info(
        "order accepted",
        extra={
            "broker_token": TOKEN,
            "order": "abc",
            "nested": {"secret": TOKEN},
            "items": [TOKEN],
        },
    )
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
    redacting_body.pop("moscow_time", None)
    plain_body.pop("moscow_time", None)
    assert redacting_body == plain_body
    assert message.encode() in redacting.encode()
    assert message.encode() in plain.encode()


def _last_payload(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out
    return json.loads(out.strip().splitlines()[-1])


def test_every_record_includes_utc_timestamp_and_moscow_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    configure("INFO", [])
    _logger().info("heartbeat ok")
    payload = _last_payload(capsys)
    assert "timestamp" in payload
    assert "moscow_time" in payload
    assert payload["level"] == "INFO"
    assert payload["logger"] == LOGGER_NAME
    assert payload["message"] == "heartbeat ok"
    utc = datetime.fromisoformat(payload["timestamp"])
    moscow = datetime.fromisoformat(payload["moscow_time"])
    assert utc.tzinfo is not None
    assert moscow.tzinfo is not None
    assert moscow.tzinfo.utcoffset(moscow) == ZoneInfo("Europe/Moscow").utcoffset(
        moscow
    )
    assert moscow == utc.astimezone(ZoneInfo("Europe/Moscow"))


def test_event_extra_field_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO", [])
    _logger().info("cycle", extra={"event": "heartbeat"})
    payload = _last_payload(capsys)
    assert payload["event"] == "heartbeat"


def test_event_field_is_redacted_when_it_contains_a_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO", [TOKEN])
    _logger().info("leak", extra={"event": f"startup_{TOKEN}"})
    out = capsys.readouterr().out
    assert TOKEN not in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert TOKEN not in payload["event"]
    assert MASK in payload["event"]
