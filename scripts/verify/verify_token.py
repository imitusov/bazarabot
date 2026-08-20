#!/usr/bin/env python3
"""V1 - token authenticates and the configured account is reachable."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, client, env, run  # noqa: E402

TOKEN = env("TINVEST_TOKEN", secret=True)
ACCOUNT_ID = env("TINVEST_ACCOUNT_ID")

v = Verifier("V1", "token and account access")


async def body():
    async with client(TOKEN) as c:
        response = await c.users.get_accounts()
        v.check("token authenticates against the live endpoint", True)

        accounts = list(response.accounts)
        ids = [a.id for a in accounts]
        found = ACCOUNT_ID in ids
        v.check("configured account is visible to this token", found,
                "{} account(s) visible".format(len(ids)))

        if found:
            account = next(a for a in accounts if a.id == ACCOUNT_ID)
            level = getattr(account.access_level, "name", str(account.access_level))
            v.check("account grants full access (not read-only)",
                    "FULL_ACCESS" in level, level)
            v.note("account name: {}".format(account.name))
            v.note("account type: {}".format(
                getattr(account.type, "name", account.type)))


run(v, body)
