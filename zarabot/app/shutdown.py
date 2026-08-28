"""Graceful shutdown. Never cancels or liquidates positions."""

from __future__ import annotations

import asyncio
import logging

from zarabot.app.loops import stop_entries
from zarabot.app.startup import AppContext
from zarabot.broker import client as broker_client
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
    """Stop entries, settle in-flight orders, close the database and channel.

    Never sells a position and never cancels a stop: a restart must have no
    financial consequence. Orders still unresolved at the timeout stay
    `SUBMITTING` for the next startup to resolve.
    """
    del ctx
    _LOG.info("shutdown requested signal=%s", signal)
    # First, before anything else. This ran concurrently with `run`, and the
    # runner was cancelled only after the drain returned — so for the whole
    # window below the trading loop kept cycling and could open a position the
    # drain had already looked past (#21).
    stop_entries()
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
    # Last, and only here. Settlement above queries the broker, so the process
    # channel (#18) has to outlive the drain it exists to serve; and closing it
    # is this module's job, because no caller closes a client it did not open.
    await broker_client.close()
