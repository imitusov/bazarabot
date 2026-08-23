#!/usr/bin/env python3
"""Open and fund a sandbox account, to find TINVEST_ACCOUNT_ID_SANDBOX.

Sandbox only. The client is hard-wired to the sandbox endpoint, so this cannot
reach the live account whatever TRADING_MODE says, and the money it pays in is
not real. Reads TINVEST_TOKEN_SANDBOX, falling back to TINVEST_TOKEN the same
way config.load() does, and never prints either.

Existing accounts are listed rather than added to: the script only opens one
when the token has none, unless --new is given.

    set -a && source .env && set +a && \
        .venv/bin/python scripts/open_sandbox_account.py [amount_roubles]
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from t_tech.invest import AsyncClient

TOKEN = (
    os.environ.get("TINVEST_TOKEN_SANDBOX") or os.environ.get("TINVEST_TOKEN") or ""
).strip()
if not TOKEN:
    print("FAIL neither TINVEST_TOKEN_SANDBOX nor TINVEST_TOKEN is set")
    sys.exit(1)


def _redact(text: object) -> str:
    out = str(text)
    if len(TOKEN) >= 8:
        out = out.replace(TOKEN, "***REDACTED***")
    return out


def _connect() -> AsyncClient:
    from t_tech.invest import AsyncClient
    from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX

    return AsyncClient(TOKEN, target=INVEST_GRPC_API_SANDBOX)


def _amount(argv: list[str]) -> int:
    for arg in argv:
        if arg != "--new":
            try:
                return int(arg)
            except ValueError:
                print(f"FAIL amount must be a whole number of roubles, got {arg!r}")
                sys.exit(1)
    raw = (os.environ.get("ALLOCATED_CAPITAL") or "10000").strip()
    return int(float(raw))


async def main() -> int:
    # T-Bank's root is not in gRPC's trust store unless this is set first.
    os.environ.setdefault("SSL_TBANK_VERIFY", "true")

    from t_tech.invest import MoneyValue
    from t_tech.invest.exceptions import AioUnauthenticatedError

    argv = sys.argv[1:]
    force_new = "--new" in argv
    amount = _amount(argv)

    try:
        async with _connect() as client:
            existing = list((await client.sandbox.get_sandbox_accounts()).accounts)
    except AioUnauthenticatedError:
        print("FAIL token rejected by the sandbox endpoint (UNAUTHENTICATED 40003).")
        print("Sandbox tokens are issued separately in the T-Invest app.")
        return 1

    if existing and not force_new:
        print(f"{len(existing)} sandbox account(s) already exist:\n")
        for account in existing:
            print(f"  TINVEST_ACCOUNT_ID_SANDBOX={account.id}")
        print("\nPut one of those in .env. Re-run with --new to open another.")
        return 0

    async with _connect() as client:
        opened = await client.sandbox.open_sandbox_account()
        account_id = opened.account_id
        print(f"opened sandbox account {account_id}")

        result = await client.sandbox.sandbox_pay_in(
            account_id=account_id,
            amount=MoneyValue(currency="rub", units=amount, nano=0),
        )
        print(f"paid in {amount} RUB, balance now {result.balance.units} RUB")

    print("\nPut this in .env:\n")
    print(f"TINVEST_ACCOUNT_ID_SANDBOX={account_id}")
    print("\nIt is read only when TRADING_MODE=sandbox; the live pair is untouched.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {type(exc).__name__}: {_redact(exc)}")
        sys.exit(1)
