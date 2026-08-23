#!/usr/bin/env python3
"""Enforce per-module coverage thresholds.

pytest-cov gates one global number. The spec requires 95% on the four modules
that decide whether money moves, and a global 80% lets those sit at 60% while
the suite stays green. Reads coverage.json and fails on any module below its
own floor.
"""

import json
import pathlib
import sys

STRICT = 95.0
STRICT_MODULES = {
    "zarabot/risk/gate.py",
    "zarabot/risk/sizing.py",
    "zarabot/lifecycle/exits.py",
    "zarabot/execution/orders.py",
}
RELAXED = 70.0
RELAXED_PREFIXES = ("zarabot/app/",)
DEFAULT = 80.0

data = json.loads(pathlib.Path("coverage.json").read_text(encoding="utf-8"))
failures = []
for path, entry in sorted(data["files"].items()):
    pct = entry["summary"]["percent_covered"]
    if path in STRICT_MODULES:
        floor, label = STRICT, "money-path"
    elif path.startswith(RELAXED_PREFIXES):
        floor, label = RELAXED, "orchestration"
    else:
        floor, label = DEFAULT, "standard"
    if pct + 1e-9 < floor:
        failures.append(f"  {path}: {pct:.1f}% < {floor:.0f}% ({label})")

if failures:
    print("FAIL per-module coverage")
    print("\n".join(failures))
    sys.exit(1)
print(f"PASS per-module coverage ({len(data['files'])} files)")
