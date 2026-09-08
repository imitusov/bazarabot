"""Tests for zarabot.telegram.notifier — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging

import pytest

from zarabot.telegram.notifier import alert

REQUIRED_ENV = {
    "TINVEST_TOKEN": "tinvest-secret-token",
    "TINVEST_ACCOUNT_ID": "acct",
    "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
    "TELEGRAM_CHAT_ID": "42",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


class _FakeBot:
    calls: list[dict[str, object]] = []
    fail_times = 0
    send_attempts = 0

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, **kwargs: object) -> None:
        _FakeBot.send_attempts += 1
        if _FakeBot.fail_times > 0:
            _FakeBot.fail_times -= 1
            raise RuntimeError("network")
        _FakeBot.calls.append(kwargs)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    _FakeBot.calls = []
    _FakeBot.fail_times = 0
    _FakeBot.send_attempts = 0
    monkeypatch.setattr("zarabot.telegram.notifier.Bot", _FakeBot)


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if getattr(record, "event", None) == event
    ]


async def test_failed_send_is_retried_then_emits_telegram_send_failed(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: Telegram outages never reach trading logic."""
    _FakeBot.fail_times = 10
    with caplog.at_level(logging.WARNING, logger="zarabot.telegram.notifier"):
        await alert("hello")
    assert _FakeBot.calls == []
    assert _FakeBot.send_attempts == 3
    events = _events(caplog, "telegram_send_failed")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.WARNING
    assert record.attempt == 3
    assert record.error == "RuntimeError"
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text


async def test_alert_body_containing_a_token_is_dropped_and_emits_secret_redacted(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """v1.61: drop the body, emit secret_redacted, send a substitute."""
    with caplog.at_level(logging.ERROR, logger="zarabot.telegram.notifier"):
        await alert("leak tinvest-secret-token please")
    events = _events(caplog, "secret_redacted")
    assert len(events) == 1
    record = events[0]
    assert record.levelno == logging.ERROR
    assert record.sink == "telegram"
    assert not hasattr(record, "length")
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text
    assert "tinvest-secret-token" not in str(record.__dict__)
    assert _FakeBot.calls
    body = str(_FakeBot.calls[-1]["text"])
    assert "tinvest-secret-token" not in body
    assert "telegram-secret-token" not in body
    assert body != "leak tinvest-secret-token please"


async def test_alert_body_never_contains_tokens(env: None) -> None:
    await alert("leak tinvest-secret-token please")
    assert _FakeBot.calls
    body = str(_FakeBot.calls[-1]["text"])
    assert "tinvest-secret-token" not in body
    assert "telegram-secret-token" not in body
    await alert("plain status")
    assert _FakeBot.calls[-1]["text"] == "plain status"
    assert _FakeBot.calls[-1]["chat_id"] == 42
