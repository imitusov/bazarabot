"""Push alerts to Telegram. Never raises; never includes a token."""

from __future__ import annotations

import logging

from telegram import Bot

from zarabot.config import load

_LOG = logging.getLogger(__name__)
_ATTEMPTS = 3
_DROPPED = "An outbound alert was dropped because it contained a secret."


def _secrets() -> tuple[str, ...]:
    cfg = load()
    return tuple(
        secret for secret in (cfg.tinvest_token, cfg.telegram_bot_token) if secret
    )


def _contains_secret(text: str) -> bool:
    return any(secret in text for secret in _secrets())


async def _send(text: str, urgent: bool) -> None:
    cfg = load()
    last_error: Exception | None = None
    for _attempt in range(_ATTEMPTS):
        try:
            bot = Bot(token=cfg.telegram_bot_token)
            await bot.send_message(
                chat_id=cfg.telegram_chat_id,
                text=text,
                disable_notification=not urgent,
            )
            return
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        _LOG.error("telegram send failed after retries")


async def alert(text: str, urgent: bool = False) -> None:
    """Send `text` to the configured chat. Never raises."""
    try:
        outbound = _DROPPED if _contains_secret(text) else text
        await _send(outbound, urgent)
    except Exception:
        _LOG.error("telegram alert failed")
