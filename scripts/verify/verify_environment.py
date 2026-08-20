#!/usr/bin/env python3
"""V9 - host readiness. Run on the VPS. Requires no token.

Checks the things the design silently assumes: a synchronised clock, because
session boundaries and candle alignment depend on it; a writable data
directory; container tooling; and outbound reachability of both external
services.
"""

import pathlib
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier, env  # noqa: E402

DATA_DIR = pathlib.Path(env("ZARABOT_DATA_DIR", required=False, default="/opt/zarabot/data"))

v = Verifier("V9", "host environment")

v.check("python is 3.12 or newer", sys.version_info >= (3, 12),
        "found {}.{}.{}".format(*sys.version_info[:3]))
v.check("docker is installed", shutil.which("docker") is not None)

compose = subprocess.run(["docker", "compose", "version"],
                         capture_output=True, text=True) if shutil.which("docker") else None
v.check("docker compose plugin is available",
        compose is not None and compose.returncode == 0)

try:
    out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                         capture_output=True, text=True, timeout=10)
    synced = out.stdout.strip() == "yes"
    v.check("system clock is NTP synchronised", synced, out.stdout.strip())
except Exception as exc:  # noqa: BLE001
    v.check("system clock is NTP synchronised", False,
            "could not query timedatectl ({})".format(type(exc).__name__))

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
