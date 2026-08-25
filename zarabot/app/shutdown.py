"""Graceful shutdown. Never cancels or liquidates positions."""

from __future__ import annotations

import asyncio
import logging

from zarabot.app.startup import AppContext
from zarabot.clock import now
from zarabot.db.connection import disconnect
from zarabot.db.orders import list_unresolved
from zarabot.db.positions import list_open
from zarabot.execution.orders import resolve_unfinished
from zarabot.telegram.notifier import alert

_LOG = logging.getLogger(__name__)
_WAIT_SECONDS = 30
_POLL_SECONDS = 1.0


async def shutdown(ctx: AppContext, signal: int) -> None:
    """Wait for in-flight orders, then return. Never sells or cancels stops."""
    del ctx
    _LOG.info("shutdown requested signal=%s", signal)
    waited = 0
    while waited < _WAIT_SECONDS:
        pending = await list_unresolved()
        if not pending:
            break
        await resolve_unfinished(now())
        if not await list_unresolved():
            break
        await asyncio.sleep(_POLL_SECONDS)
        waited += int(_POLL_SECONDS)
    remaining = await list_unresolved()
    opened = await list_open()
    _LOG.info(
        "shutdown complete unresolved=%s open_positions=%s",
        len(remaining),
        len(opened),
    )
    await alert(
        f"zarabot shutting down signal={signal} "
        f"unresolved={len(remaining)} positions={len(opened)}"
    )
    await disconnect()
