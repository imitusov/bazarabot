#!/usr/bin/env python3
"""Enforce per-module coverage thresholds.

pytest-cov gates one global number. The spec requires 95% on the four modules
that decide whether money moves, and a global 80% lets those sit at 60% while
the suite stays green. Reads coverage.json and fails on any module below its
own floor.

`DEFAULT` here is the PER-FILE floor. The project total of the same value is
`fail_under` in pyproject.toml, enforced by coverage itself; AGENTS.md states
both and `check_rulebook.py` compares all three, because two numbers reading 80
for different reasons drift apart the first time one of them is changed.

WHAT THIS DOES NOT COVER, stated because a floor described as protecting the
money path must say what it leaves unprotected (failure class 6):

  * That a `STRICT_MODULES` path still exists. The floors are applied by
    matching the paths coverage.json reports; a renamed or moved money-path
    module simply stops matching, falls to `DEFAULT`, and the 95% floor
    evaporates with no FAIL anywhere — `check_rulebook.py` compares this set
    against AGENTS.md, and neither of them opens the filesystem. #117 item 9c.
  * Modules absent from coverage.json entirely. A file never imported by the
    suite has no entry, so it has no floor either; the loop below iterates
    what coverage found, not what exists.
  * Branch coverage as such. `percent_covered` is whatever
    `[tool.coverage.run]` was configured to measure.
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

# Modules pinned at the coverage they had measured when per-module floors were
# introduced. Both values are ABOVE `DEFAULT`, so this is a STRICTER floor than
# the standard one, not an exemption from it — the name is historical and the
# first line of this comment said "already below the default" until #117 item 8b,
# which was false of both entries and read as a waiver. Recorded at their real
# value so the gate ratchets: they may improve, they may not get worse, and the
# figure is visible here instead of being hidden by a global average that the
# rest of the project pays for. See issue #30.
KNOWN_BELOW = {
    "zarabot/broker/client.py": 86.0,
    "zarabot/pnl.py": 85.0,
}

data = json.loads(pathlib.Path("coverage.json").read_text(encoding="utf-8"))
failures = []
for path, entry in sorted(data["files"].items()):
    pct = entry["summary"]["percent_covered"]
    if path in STRICT_MODULES:
        floor, label = STRICT, "money-path"
    elif path in KNOWN_BELOW:
        floor, label = KNOWN_BELOW[path], "ratchet, see #30"
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
