#!/usr/bin/env python3
"""List the brokerage accounts a token can see, to find TINVEST_ACCOUNT_ID.

Read-only: lists accounts and their balances. Places no orders and changes
nothing. Reads TINVEST_TOKEN from the environment and never prints it.

    set -a && source .env && set +a && .venv/bin/python scripts/list_accounts.py
"""

import asyncio
import os
import sys

TOKEN = os.environ.get("TINVEST_TOKEN")
if not TOKEN:
    print("FAIL TINVEST_TOKEN is not set")
    sys.exit(1)


def _name(value: object) -> str:
    return str(getattr(value, "name", value))


async def main() -> int:
    from t_tech.invest import AsyncClient
    from t_tech.invest.utils import money_to_decimal

    async with AsyncClient(TOKEN) as client:
        accounts = list((await client.users.get_accounts()).accounts)

    if not accounts:
        print("No accounts visible to this token.")
        return 1

    print(f"{len(accounts)} account(s) visible to this token:\n")
    for account in accounts:
        access = _name(account.access_level)
        tradeable = "FULL_ACCESS" in access
        print(f"  id           {account.id}")
        print(f"  name         {account.name}")
        print(f"  type         {_name(account.type)}")
        print(f"  status       {_name(account.status)}")
        print("  access       {}{}".format(
            access, "" if tradeable else "   <- cannot trade with this token"))
        print("  opened       {}".format(
            account.opened_date.date() if account.opened_date else "?"))

        if tradeable and "OPEN" in _name(account.status):
            try:
                async with AsyncClient(TOKEN) as client:
                    portfolio = await client.operations.get_portfolio(
                        account_id=account.id)
                total = money_to_decimal(portfolio.total_amount_portfolio)
                cash = money_to_decimal(portfolio.total_amount_currencies)
                print(f"  value        {total} RUB total, {cash} RUB cash, "
                      f"{len(list(portfolio.positions))} position(s)")
            except Exception as exc:  # noqa: BLE001
                print(f"  value        unavailable ({type(exc).__name__})")
        print()

    print("Pick the SEPARATE account you funded with the bot's capital and")
    print("nothing else, then put its id in .env as TINVEST_ACCOUNT_ID.")
    print("It must show FULL_ACCESS. Do not point this at your main savings:")
    print("the daily loss limit measures against that account's own equity.")
    return 0


sys.exit(asyncio.run(main()))
