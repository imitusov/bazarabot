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

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, **kwargs: object) -> None:
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
    monkeypatch.setattr("zarabot.telegram.notifier.Bot", _FakeBot)


async def test_failed_send_is_retried_then_logged_without_raising(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    _FakeBot.fail_times = 10
    with caplog.at_level(logging.ERROR):
        await alert("hello")
    assert _FakeBot.calls == []
    assert caplog.records
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text


async def test_alert_body_never_contains_tokens(env: None) -> None:
    await alert("leak tinvest-secret-token please")
    assert _FakeBot.calls
    body = str(_FakeBot.calls[-1]["text"])
    assert "tinvest-secret-token" not in body
    assert "telegram-secret-token" not in body
    await alert("plain status")
    assert _FakeBot.calls[-1]["text"] == "plain status"
    assert _FakeBot.calls[-1]["chat_id"] == 42
