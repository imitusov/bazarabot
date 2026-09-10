"""Tests for zarabot.telegram.notifier — written from technical-spec.md §3.2."""

from __future__ import annotations

import logging

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from zarabot.telegram.notifier import alert

REQUIRED_ENV = {
    "TINVEST_TOKEN": "tinvest-secret-token",
    "TINVEST_ACCOUNT_ID": "tinvest-account-id-secret",
    "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
    "TELEGRAM_CHAT_ID": "42",
    "ALLOCATED_CAPITAL": "100000",
    "WATCHLIST": "SBER",
}


class _FakeBot:
    calls: list[dict[str, object]] = []
    fail_times = 0
    send_attempts = 0
    failure: BaseException = NetworkError("network")

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, **kwargs: object) -> None:
        _FakeBot.send_attempts += 1
        if _FakeBot.fail_times > 0:
            _FakeBot.fail_times -= 1
            raise _FakeBot.failure
        _FakeBot.calls.append(kwargs)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    _FakeBot.calls = []
    _FakeBot.fail_times = 0
    _FakeBot.send_attempts = 0
    _FakeBot.failure = NetworkError("network")
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
    assert record.error == "NetworkError"
    assert "tinvest-secret-token" not in caplog.text
    assert "telegram-secret-token" not in caplog.text
    assert "tinvest-account-id-secret" not in caplog.text


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
    assert "tinvest-account-id-secret" not in caplog.text
    assert "tinvest-secret-token" not in str(record.__dict__)
    assert "tinvest-account-id-secret" not in str(record.__dict__)
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


async def test_alert_body_containing_account_id_is_dropped_and_emits_secret_redacted(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 19: the brokerage account identifier is a secret too."""
    with caplog.at_level(logging.ERROR, logger="zarabot.telegram.notifier"):
        await alert("leak tinvest-account-id-secret please")
    events = _events(caplog, "secret_redacted")
    assert len(events) == 1
    record = events[0]
    assert record.sink == "telegram"
    assert not hasattr(record, "length")
    assert "tinvest-account-id-secret" not in caplog.text
    assert "tinvest-account-id-secret" not in str(record.__dict__)
    body = str(_FakeBot.calls[-1]["text"])
    assert "tinvest-account-id-secret" not in body
    assert body != "leak tinvest-account-id-secret please"


# --- Rule 13 (v1.75): the catch is narrow. ------------------------------------
# "A send failure is `telegram.error.TelegramError` and only that; the retries
#  - three - are for `telegram.error.NetworkError` (including `TimedOut`) and
#  `telegram.error.RetryAfter` ... `BadRequest`, `Forbidden` and `InvalidToken`
#  ... logged once, not retried. **Every other exception propagates** out of
#  `alert` to the caller, and thence to rule 21."


async def test_timed_out_is_retried_three_times(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """`TimedOut` is a `NetworkError`: a transport failure a retry can fix."""
    _FakeBot.failure = TimedOut()
    _FakeBot.fail_times = 10
    with caplog.at_level(logging.WARNING, logger="zarabot.telegram.notifier"):
        await alert("hello")
    assert _FakeBot.send_attempts == 3
    assert len(_events(caplog, "telegram_send_failed")) == 1


async def test_retry_after_is_retried_three_times(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    _FakeBot.failure = RetryAfter(1)
    _FakeBot.fail_times = 10
    with caplog.at_level(logging.WARNING, logger="zarabot.telegram.notifier"):
        await alert("hello")
    assert _FakeBot.send_attempts == 3


async def test_bad_request_is_logged_once_and_never_retried(
    env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A settings failure is just as wrong on the third attempt.

    `BadRequest` subclasses `NetworkError` in python-telegram-bot, so a retry
    predicate written as `isinstance(exc, NetworkError)` would retry it. It
    must not.
    """
    _FakeBot.failure = BadRequest("chat not found")
    _FakeBot.fail_times = 10
    with caplog.at_level(logging.WARNING, logger="zarabot.telegram.notifier"):
        await alert("hello")
    assert _FakeBot.send_attempts == 1
    events = _events(caplog, "telegram_send_failed")
    assert len(events) == 1
    assert events[0].error == "BadRequest"
    assert events[0].attempt == 1


async def test_non_telegram_exception_propagates_out_of_alert(env: None) -> None:
    """The other direction, and the point of the amendment.

    A rename inside the library, or an `AttributeError` in the message-building
    code, is not a send failure. It must reach rule 21's supervisor with its
    traceback rather than be absorbed by the rule that exists for network
    flakiness. This test is red against `except Exception`.
    """
    _FakeBot.failure = AttributeError("Bot has no attribute send_message")
    _FakeBot.fail_times = 10
    with pytest.raises(AttributeError):
        await alert("hello")
    assert _FakeBot.send_attempts == 1


async def test_defect_in_alert_itself_propagates(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer catch in `alert` is narrow too, not only `_send`'s."""

    def _boom() -> tuple[str, ...]:
        raise TypeError("config renamed")

    monkeypatch.setattr("zarabot.telegram.notifier._secrets", _boom)
    with pytest.raises(TypeError):
        await alert("hello")
