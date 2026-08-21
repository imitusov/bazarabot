#!/usr/bin/env python3
"""List the brokerage accounts a token can see, to find TINVEST_ACCOUNT_ID.

Read-only: lists accounts and their balances. Places no orders and changes
nothing. Reads TINVEST_TOKEN from the environment and never prints it.

    set -a && source .env && set +a && .venv/bin/python scripts/list_accounts.py
"""

from __future__ import annotations

import asyncio
import os
import sys

TOKEN = (os.environ.get("TINVEST_TOKEN") or "").strip()
if not TOKEN:
    print("FAIL TINVEST_TOKEN is not set")
    sys.exit(1)


def _name(value: object) -> str:
    return str(getattr(value, "name", value))


def _mode() -> str:
    raw = (os.environ.get("TRADING_MODE") or "live").strip().split()[0]
    return raw if raw in {"live", "sandbox"} else "live"


def _redact(text: object) -> str:
    out = str(text)
    if len(TOKEN) >= 8:
        out = out.replace(TOKEN, "***REDACTED***")
    return out


def _connect(sandbox: bool):
    from t_tech.invest import AsyncClient
    from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX

    if sandbox:
        return AsyncClient(TOKEN, target=INVEST_GRPC_API_SANDBOX)
    return AsyncClient(TOKEN)


async def _accounts(sandbox: bool) -> list[object]:
    async with _connect(sandbox) as client:
        return list((await client.users.get_accounts()).accounts)


async def _print_account(account: object, sandbox: bool) -> None:
    from t_tech.invest.exceptions import AioUnauthenticatedError
    from t_tech.invest.utils import money_to_decimal

    access = _name(account.access_level)
    tradeable = "FULL_ACCESS" in access
    print(f"  id           {account.id}")
    print(f"  name         {account.name}")
    print(f"  type         {_name(account.type)}")
    print(f"  status       {_name(account.status)}")
    print(
        "  access       {}{}".format(
            access, "" if tradeable else "   <- cannot trade with this token"
        )
    )
    print(
        "  opened       {}".format(
            account.opened_date.date() if account.opened_date else "?"
        )
    )
    if tradeable and "OPEN" in _name(account.status):
        try:
            async with _connect(sandbox) as client:
                portfolio = await client.operations.get_portfolio(
                    account_id=account.id
                )
            total = money_to_decimal(portfolio.total_amount_portfolio)
            cash = money_to_decimal(portfolio.total_amount_currencies)
            print(
                f"  value        {total} RUB total, {cash} RUB cash, "
                f"{len(list(portfolio.positions))} position(s)"
            )
        except AioUnauthenticatedError:
            print("  value        unavailable (token rejected)")
        except Exception as exc:  # noqa: BLE001
            print(f"  value        unavailable ({type(exc).__name__})")
    print()


async def main() -> int:
    # T-Bank's root is not in gRPC's trust store unless this is set first.
    os.environ.setdefault("SSL_TBANK_VERIFY", "true")

    from t_tech.invest.exceptions import AioUnauthenticatedError

    configured = _mode()
    sandbox = configured == "sandbox"
    try:
        accounts = await _accounts(sandbox)
        used = configured
    except AioUnauthenticatedError:
        other = "live" if sandbox else "sandbox"
        print(
            f"FAIL token rejected on the {configured} endpoint (UNAUTHENTICATED 40003)."
        )
        try:
            accounts = await _accounts(not sandbox)
        except AioUnauthenticatedError:
            print(f"FAIL token also rejected on the {other} endpoint.")
            print(
                "Issue a new full-access token in the T-Invest app, put it in "
                ".env as TINVEST_TOKEN (no quotes, no spaces), and re-run."
            )
            return 1
        used = other
        print(
            f"The token works on {other}. Set TRADING_MODE={other} in .env "
            "to match, then re-run.\n"
        )

    if not accounts:
        print("No accounts visible to this token.")
        return 1

    print(f"{len(accounts)} account(s) on {used}:\n")
    for account in accounts:
        await _print_account(account, used == "sandbox")

    print("Pick the SEPARATE account you funded with the bot's capital and")
    print("nothing else, then put its id in .env as TINVEST_ACCOUNT_ID.")
    print("It must show FULL_ACCESS. Do not point this at your main savings:")
    print("the daily loss limit measures against that account's own equity.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
