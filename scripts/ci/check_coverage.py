#!/usr/bin/env python3
"""Enforce per-module coverage thresholds, and that they apply to real files.

pytest-cov gates one global number. The spec requires 95% on the four modules
that decide whether money moves, and a global 80% lets those sit at 60% while
the suite stays green. Reads coverage.json and fails on any module below its
own floor.

`DEFAULT` here is the PER-FILE floor. The project total of the same value is
`fail_under` in pyproject.toml, enforced by coverage itself; AGENTS.md states
both and `check_rulebook.py` compares all three, because two numbers reading 80
for different reasons drift apart the first time one of them is changed.

The floors are applied by matching the paths coverage.json reports, so an entry
in a floor table is only worth the path it names. Issue #219: a renamed or
moved money-path module used to match nothing, fall through to `DEFAULT`, and
leave this gate printing PASS — the strictest check in the project switching
itself off with no symptom (failure class 6). `check_tables` now FAILs, naming
the entry, when a `STRICT_MODULES`, `KNOWN_BELOW` or `RELAXED_PREFIXES` entry
resolves to nothing on disk, when a named module is missing from coverage.json
(present but never imported, so it got no floor at all), or when one path is
listed in both tables (`STRICT` wins the branch below, so the `KNOWN_BELOW`
entry would be dead).

Every input is parsed behind `InputError` and reported as a FAIL line: a
`KeyError` from inside a parse is a worse diagnostic than a named failure.

WHAT THIS DOES NOT COVER, stated because a floor described as protecting the
money path must say what it leaves unprotected (failure class 6):

  * Modules NOT in either floor table that are absent from coverage.json. A
    file never imported by the suite has no entry, so it has no floor either;
    the loop below iterates what coverage found, not what exists. Only the
    named money-path and ratchet modules are checked in that direction.
  * Whether coverage.json describes THIS tree. It is read as given: a stale
    report from an older run, or one generated with a different
    `[tool.coverage.run]`, passes exactly as if it were current.
  * Branch coverage as such, and WHICH branches. `percent_covered` is whatever
    coverage was configured to measure; a money-path module at 96% with its
    loss-making branch uncovered passes.
  * The project total. That is coverage's own `fail_under`, and the two are
    enforced separately on purpose (see above).
  * Whether the floors are the RIGHT floors. `check_rulebook.py` compares them
    against AGENTS.md's Coverage sentence; neither file judges the numbers.
  * Ratcheting `KNOWN_BELOW` upward. The pins are hand-edited, so a module now
    measuring 92% keeps its 86% floor until someone raises it — the ratchet
    stops a regression, it does not capture an improvement.
  * That `RELAXED_PREFIXES` matches any file. The directory must exist; an
    empty `zarabot/app/` would pass.
"""

from __future__ import annotations

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


class InputError(ValueError):
    """An input moved — reported as a FAIL line, never as a traceback."""


def coverage_percentages(root: pathlib.Path) -> dict[str, float]:
    """`{path: percent_covered}` from coverage.json, or raise `InputError`."""
    path = root / "coverage.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(
            f"cannot read {path} ({exc.strerror}) — run "
            "`pytest --cov --cov-report=json` first; a coverage gate with no "
            "coverage report proves nothing"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"coverage.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise InputError(
            "coverage.json has no 'files' object — the report format changed, "
            "so no per-module floor was applied"
        )
    percentages: dict[str, float] = {}
    for name, entry in data["files"].items():
        try:
            percentages[name] = float(entry["summary"]["percent_covered"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(
                f"coverage.json entry {name!r} has no numeric "
                f"summary.percent_covered ({exc!r})"
            ) from exc
    if not percentages:
        raise InputError("coverage.json reports no files")
    return percentages


def check_tables(
    root: pathlib.Path, lines: list[str], reported: dict[str, float] | None = None
) -> None:
    """Issue #219: every floor-table entry must name something that exists.

    `reported` is coverage.json's file map when it could be read. The
    on-disk half runs regardless, so a missing report cannot hide an entry
    that names nothing.
    """
    for path in sorted(STRICT_MODULES):
        if not (root / path).is_file():
            lines.append(
                f"FAIL STRICT_MODULES entry {path!r} names no file — the "
                f"{STRICT:.0f}% money-path floor is applied to nothing and "
                f"whatever replaced it falls to {DEFAULT:.0f}%"
            )
    for path in sorted(KNOWN_BELOW):
        if not (root / path).is_file():
            lines.append(
                f"FAIL KNOWN_BELOW entry {path!r} names no file — its "
                f"{KNOWN_BELOW[path]:.0f}% ratchet floor (issue #30) is applied "
                "to nothing"
            )
    for prefix in RELAXED_PREFIXES:
        if not (root / prefix).is_dir():
            lines.append(
                f"FAIL RELAXED_PREFIXES entry {prefix!r} names no directory — "
                f"the {RELAXED:.0f}% orchestration floor is applied to nothing"
            )
    for path in sorted(STRICT_MODULES & set(KNOWN_BELOW)):
        lines.append(
            f"FAIL {path!r} is in both STRICT_MODULES and KNOWN_BELOW — STRICT "
            "wins, so the ratchet entry is dead; delete one"
        )
    if reported is None:
        return
    for path in sorted(STRICT_MODULES):
        if (root / path).is_file() and path not in reported:
            lines.append(
                f"FAIL STRICT_MODULES entry {path!r} has no coverage.json entry "
                "— the file exists but the suite never imported it, so no floor "
                "was applied to it at all"
            )
    for path in sorted(KNOWN_BELOW):
        if (root / path).is_file() and path not in reported:
            lines.append(
                f"FAIL KNOWN_BELOW entry {path!r} has no coverage.json entry — "
                "the file exists but the suite never imported it, so its "
                "ratchet floor was applied to nothing"
            )


def floor_for(path: str) -> tuple[float, str]:
    """The floor this reported path must clear, and why it carries it."""
    if path in STRICT_MODULES:
        return STRICT, "money-path"
    if path in KNOWN_BELOW:
        return KNOWN_BELOW[path], "ratchet, see #30"
    if path.startswith(RELAXED_PREFIXES):
        return RELAXED, "orchestration"
    return DEFAULT, "standard"


def check_floors(reported: dict[str, float], lines: list[str]) -> None:
    for path, pct in sorted(reported.items()):
        floor, label = floor_for(path)
        if pct + 1e-9 < floor:
            lines.append(f"FAIL {path}: {pct:.1f}% < {floor:.0f}% ({label})")


def evaluate(root: pathlib.Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    reported: dict[str, float] | None = None
    try:
        reported = coverage_percentages(root)
    except InputError as exc:
        lines.append(f"FAIL {exc}")
    check_tables(root, lines, reported)
    if reported is None:
        return 1, lines
    check_floors(reported, lines)
    if any(line.startswith("FAIL") for line in lines):
        return 1, lines
    lines.append(
        f"PASS per-module coverage ({len(reported)} files; "
        f"{len(STRICT_MODULES)} at {STRICT:.0f}%, "
        f"{len(KNOWN_BELOW)} ratcheted, all present)"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
