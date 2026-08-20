"""Shared harness for the pre-development verification suite.

Contract (technical-spec.md §2): every script prints exactly one summary line
beginning PASS or FAIL, and exits 0 on pass, 1 on failure. Partial success is
failure — a script verifying three things and confirming two exits 1.

No script ever prints a token, in any form, including inside a traceback.
Secrets are registered here and masked out of every line this module emits.
"""

import asyncio
import os
import sys

_SECRETS = set()


def register_secret(value):
    """Values registered here are masked from all harness output."""
    if value and len(str(value)) >= 8:
        _SECRETS.add(str(value))


def mask(text):
    out = str(text)
    for secret in _SECRETS:
        out = out.replace(secret, "***REDACTED***")
    return out


def env(name, required=True, default=None, secret=False):
    value = os.environ.get(name, default)
    if secret:
        register_secret(value)
    if required and not value:
        print("FAIL {}: required environment variable is not set".format(name))
        sys.exit(1)
    return value


def env_list(name, required=True):
    raw = env(name, required=required, default="")
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


class Verifier:
    """Accumulates checks and emits the single PASS/FAIL summary line."""

    def __init__(self, code, title):
        self.code = code
        self.title = title
        self.results = []
        print("{} — {}".format(code, title))

    def check(self, label, ok, detail=""):
        ok = bool(ok)
        self.results.append((label, ok))
        marker = " ok " if ok else "FAIL"
        line = "  [{}] {}".format(marker, label)
        if detail:
            line += " — {}".format(mask(detail))
        print(line)
        return ok

    def note(self, text):
        print("   .   {}".format(mask(text)))

    def crashed(self, exc):
        """Record an unexpected exception as a failed check, never a traceback."""
        self.results.append(("unexpected error", False))
        print("  [FAIL] unexpected error — {}: {}".format(
            type(exc).__name__, mask(exc)))

    def finish(self):
        total = len(self.results)
        passed = sum(1 for _, ok in self.results if ok)
        ok = total > 0 and passed == total
        print("{} {} — {} ({}/{} checks passed)".format(
            "PASS" if ok else "FAIL", self.code, self.title, passed, total))
        sys.exit(0 if ok else 1)


def run(verifier, coro_factory):
    """Run an async verification body, converting any escape into a FAIL."""
    try:
        asyncio.run(coro_factory())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - deliberate: nothing may escape
        verifier.crashed(exc)
    verifier.finish()


def client(token, sandbox=False):
    """AsyncClient against the live or sandbox endpoint.

    The sandbox is selected by endpoint, not by a different set of methods, so
    the same calls exercise the same code path in both. Using the
    post_sandbox_* family instead would mean sandbox testing proves nothing
    about the live path.
    """
    from t_tech.invest import AsyncClient
    from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX

    if sandbox:
        return AsyncClient(token, target=INVEST_GRPC_API_SANDBOX)
    return AsyncClient(token)
