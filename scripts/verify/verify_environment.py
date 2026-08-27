#!/usr/bin/env python3
"""V9 - host readiness. Run on the VPS. Requires no token.

Checks the things the design silently assumes: a synchronised clock, because
session boundaries and candle alignment depend on it; a writable data
directory; container tooling; and outbound reachability of both external
services.
"""

import pathlib
import platform
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, env  # noqa: E402

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DATA_DIR = pathlib.Path(env("ZARABOT_DATA_DIR", required=False,
                            default=str(_REPO_ROOT / "data")))

MAX_CLOCK_OFFSET_SECONDS = 2.0

v = Verifier("V9", "host environment")

v.check("python is 3.12 or newer", sys.version_info >= (3, 12),
        "found {}.{}.{}".format(*sys.version_info[:3]))
v.check("docker is installed", shutil.which("docker") is not None)

compose = subprocess.run(["docker", "compose", "version"],
                         capture_output=True, text=True) if shutil.which("docker") else None
v.check("docker compose plugin is available",
        compose is not None and compose.returncode == 0)

# The clock check is about the DEPLOYMENT host (rule 29: V9 confirms it before
# the bot is deployed). timedatectl is systemd, so on a macOS development
# machine it does not exist and the whole suite stopped here — which is why
# `make verify` had never completed. Ask the platform's own time daemon, and
# say plainly which host was measured.
def _clock_synchronised():
    if shutil.which("timedatectl"):
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized",
                              "--value"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "yes", out.stdout.strip()
    if shutil.which("sntp"):
        # Measures the thing rule 29 actually cares about — how far this clock
        # is from true — rather than whether a daemon is enabled. Non-privileged;
        # `systemsetup -getusingnetworktime` needs admin and cannot run here.
        out = subprocess.run(["sntp", "-t", "5", "time.apple.com"],
                             capture_output=True, text=True, timeout=20)
        text = (out.stdout or out.stderr).strip().splitlines()[-1:]
        line = text[0] if text else ""
        try:
            offset = abs(float(line.split()[0]))
        except (IndexError, ValueError):
            return False, "could not parse sntp output: {}".format(line[:80])
        return offset < MAX_CLOCK_OFFSET_SECONDS, (
            "offset {:+.3f}s (limit {}s) — {}".format(
                offset, MAX_CLOCK_OFFSET_SECONDS, line[:60]))
    return False, "no supported time daemon query on this platform"


try:
    synced, detail = _clock_synchronised()
    v.check("system clock is NTP synchronised on {}".format(platform.node()),
            synced, detail)
except Exception as exc:  # noqa: BLE001
    v.check("system clock is NTP synchronised", False,
            "could not query the time daemon ({})".format(type(exc).__name__))

DATA_DIR.mkdir(parents=True, exist_ok=True)
probe = DATA_DIR / ".write-probe"
try:
    probe.write_text("ok")
    probe.unlink()
    writable = True
except Exception:  # noqa: BLE001
    writable = False
v.check("data directory is writable", writable, str(DATA_DIR))

for label, host in (("broker API", "invest-public-api.tbank.ru"),
                    ("broker sandbox", "sandbox-invest-public-api.tbank.ru"),
                    ("telegram", "api.telegram.org")):
    try:
        socket.create_connection((host, 443), timeout=10).close()
        reachable = True
    except Exception:  # noqa: BLE001
        reachable = False
    v.check("{} is reachable on 443".format(label), reachable, host)

v.finish()
