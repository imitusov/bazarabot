"""Push alerts to Telegram. Never raises a send failure; never includes a token.

Rule 13 (v1.75): a send failure is `telegram.error.TelegramError` and only
that. Every other exception propagates to the caller and thence to rule 21.
An unqualified "never raises" here means a rename inside this module silences
every alert in the system while each module believes it has spoken.
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from zarabot.config import load

_LOG = logging.getLogger(__name__)
_ATTEMPTS = 3
_DROPPED = "An outbound alert was dropped because it contained a secret."


def _is_transient(exc: TelegramError) -> bool:
    """Rule 13 (v1.75): retry the transport failures, and only those.

    `NetworkError` (including its `TimedOut`) and `RetryAfter` are what a
    second attempt can fix. `BadRequest`, `Forbidden` and `InvalidToken` are
    settings that will be equally wrong on the third attempt. `BadRequest`
    subclasses `NetworkError` in python-telegram-bot, so it is excluded here
    explicitly rather than by the class check alone.
    """
    if isinstance(exc, BadRequest):
        return False
    return isinstance(exc, NetworkError | RetryAfter)


def _secrets() -> tuple[str, ...]:
    cfg = load()
    return tuple(
        secret
        for secret in (
            cfg.tinvest_token,
            cfg.telegram_bot_token,
            cfg.tinvest_account_id,
        )
        if secret
    )


def _contains_secret(text: str) -> bool:
    return any(secret in text for secret in _secrets())


async def _send(text: str, urgent: bool) -> None:
    cfg = load()
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            bot = Bot(token=cfg.telegram_bot_token)
            await bot.send_message(
                chat_id=cfg.telegram_chat_id,
                text=text,
                disable_notification=not urgent,
            )
            return
        except TelegramError as exc:
            # Rule 13 (v1.75): a send failure is `telegram.error.TelegramError`
            # and only that. Every other exception propagates out of `alert` to
            # the caller, and thence to rule 21's supervisor with its
            # traceback — a rename here must not silence the whole channel.
            if attempt == _ATTEMPTS or not _is_transient(exc):
                _LOG.warning(
                    "telegram_send_failed",
                    extra={
                        "event": "telegram_send_failed",
                        "attempt": attempt,
                        "error": type(exc).__name__,
                    },
                )
                return


async def alert(text: str, urgent: bool = False) -> None:
    """Send `text` to the configured chat. Never raises a `TelegramError`."""
    try:
        if _contains_secret(text):
            _LOG.error(
                "secret_redacted",
                extra={"event": "secret_redacted", "sink": "telegram"},
            )
            outbound = _DROPPED
        else:
            outbound = text
        await _send(outbound, urgent)
    except TelegramError:
        # Rule 13 (v1.75), same narrowing: `_send` already absorbs the send
        # failure, so this is the belt on the same contract's braces. A defect
        # anywhere in this function propagates.
        _LOG.error("telegram alert failed")
