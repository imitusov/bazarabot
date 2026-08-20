"""Process entry so that `python -m zarabot` starts the bot."""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import suppress

from zarabot.app.loops import run
from zarabot.app.shutdown import shutdown
from zarabot.app.startup import StartupError, start


async def _amain() -> int:
    try:
        ctx = await start()
    except StartupError:
        await asyncio.sleep(30)
        return 1

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    async def _handle(sig: int) -> None:
        await shutdown(ctx, int(sig))
        stop.set()

    def _schedule(sig: int) -> None:
        loop.create_task(_handle(sig))

    loop.add_signal_handler(signal.SIGTERM, lambda: _schedule(signal.SIGTERM))
    loop.add_signal_handler(signal.SIGINT, lambda: _schedule(signal.SIGINT))

    runner = asyncio.create_task(run(ctx))
    waiter = asyncio.create_task(stop.wait())
    await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    runner.cancel()
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await runner
    with suppress(asyncio.CancelledError):
        await waiter
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
