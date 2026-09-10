#!/usr/bin/env python3
"""Fail when technical-spec.md §3.2 and the `M` table in make_tasks.py disagree.

Issue #118 item 5 (test-contract reachability). `scripts/make_tasks.py` cuts
§3.2 by exact bold heading: `M`'s fourth field is the heading text, or `None`
meaning "this module has no test contract", which prints "No dedicated test
block in §3.2. Derive cases from the contract above". Both halves of that
arrangement can be wrong silently, and both have been:

  * a §3.2 block reachable from no `M` entry — written cases no agent is ever
    shown (`sandbox.exchange`, still live; and `partial fills`, eleven cases
    for the highest-risk module in the project);
  * a `None` key whose §3.2 block exists — the task file tells the agent to
    invent cases the spec already wrote (#114, `ops.commissions`, fixed as a
    single datum by #156 with no check behind it);
  * a key naming a heading that does not exist — the same fallback text, but
    reached by a typo or a renamed block rather than by a `None`.

This gate checks all three directions, over the heading set §3.2 actually
contains, matched by the same rule `test_block` uses (a line whose strip is
exactly ``**text**``).

WHAT THIS DOES NOT COVER, stated because a guard described as proving
completeness must enumerate what it leaves out (failure class 6):

  * That every §4 module HAS a test contract. #113 is exactly this:
    `db.stop_orders` has a full §4 contract, no §3.2 block, and a `None` key.
    That is a *consistent* state — the generator's claim about the spec is
    true — so this gate is deliberately silent on it. The missing contract is
    a spec gap tracked by #113, and closing it is a spec amendment, not a
    reachability fix. §4-vs-`M` coverage is issue #118's gate 4.
  * Block CONTENT. A heading followed by one vacuous line passes. Whether the
    listed cases are the right cases, or map to real tests, is the critic's
    job and the traceability check's, not this one's.
  * Sub-blocks that are not standalone bold lines. §3.2's
    ``**stop-order lifecycle** (`execution.orders`, `broker.reconcile`)``
    carries trailing prose, so neither `test_block` nor this gate sees it as
    a heading — it is swallowed into whichever block precedes it. This gate
    mirrors the generator's blindness rather than correcting it.
  * The generated task FILES. It re-derives the key set from `M`; that the
    files on disk match is the `drift` target's job (`make_tasks.py` +
    `git diff --exit-code tasks/`).
  * `M`'s other fields — §4 heading fragment, tables, error rules. Those are
    other gates.

A green run here means every §3.2 block reaches a task file and every task
file's claim about its own test block is true — NOT that every module has a
test contract, and not that the contracts it does have are any good.

The allowlist is an inventory of the orphans that existed when the gate was
written. Every entry names its issue, and an entry that stops describing a
real gap FAILS rather than being ignored: a stale waiver is a silent
exemption, which is how #124's and #180's entries were caught.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# §3.2 blocks reachable from no `M` entry today. Key is the heading text as it
# appears between the asterisks. Delete an entry with its issue.
KNOWN_UNREACHED_TEST_BLOCKS: dict[str, str] = {
    "`sandbox.exchange`": (
        "#114/#118 — sandbox/exchange.py has a §3.2 block and no `M` entry at "
        "all; adding the entry is issue #118 gate 4's territory"
    ),
    "partial fills": (
        "#118 — eleven execution.orders cases under their own bold heading, so "
        "`test_block` stops before them and tasks/26 never receives them; the "
        "fix is a spec amendment folding them under `execution.orders`"
    ),
}

_H32 = re.compile(r"(?m)^### 3\.2[ .]")
_H4 = re.compile(r"(?m)^## 4\.[ ]")
_BOLD_LINE = re.compile(r"^\*\*(.+)\*\*$")


def spec_headings(spec: str) -> list[str]:
    """Every §3.2 test-contract heading, in document order.

    Raises ValueError when either anchor has moved. A moved anchor must be a
    failure and never an empty result: two gates in this repo shipped that
    defect (#165, #163) and passed with zero parsed rows.
    """
    start = _H32.search(spec)
    if start is None:
        raise ValueError(
            "technical-spec.md has no '### 3.2 ...' heading — the test-contract "
            "section this gate reads has moved or been renamed"
        )
    rest = spec[start.end() :]
    end = _H4.search(rest)
    if end is None:
        raise ValueError(
            "technical-spec.md has no '## 4. ' heading after §3.2 — the anchor "
            "that bounds the test-contract section has moved or been renamed"
        )
    body = rest[: end.start()]
    headings: list[str] = []
    for line in body.splitlines():
        hit = _BOLD_LINE.match(line.strip())
        if hit is not None:
            headings.append(hit.group(1))
    if not headings:
        raise ValueError(
            "technical-spec.md §3.2 contains no test-contract headings — a "
            "parse that finds nothing is a broken parse, not a clean tree"
        )
    return headings


def module_keys(make_tasks_src: str) -> list[tuple[str, str | None]]:
    """`(module name, test-contract key)` for every row of `M`.

    Parsed with `ast`, never imported: importing `make_tasks` rewrites
    `tasks/`, and a gate with a side effect on a tracked directory is a gate
    that can mask the drift check standing next to it.
    """
    tree = ast.parse(make_tasks_src)
    table: object | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "M":
            table = ast.literal_eval(node.value)
            break
    if table is None:
        raise ValueError(
            "scripts/make_tasks.py has no module-level `M = [...]` table — the "
            "generator table this gate reads has moved or been renamed"
        )
    if not isinstance(table, list) or not table:
        raise ValueError("scripts/make_tasks.py `M` is empty")
    rows: list[tuple[str, str | None]] = []
    bad = "scripts/make_tasks.py `M` has a malformed row: "
    for entry in table:
        if not isinstance(entry, tuple) or len(entry) < 4:
            raise ValueError(bad + repr(entry))
        name, key = entry[1], entry[3]
        if not isinstance(name, str) or not (key is None or isinstance(key, str)):
            raise ValueError(bad + repr(entry))
        rows.append((name, key))
    return rows


def evaluate(
    root: pathlib.Path,
    *,
    unreached: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    allow = KNOWN_UNREACHED_TEST_BLOCKS if unreached is None else unreached
    lines: list[str] = []
    spec = (root / "technical-spec.md").read_text(encoding="utf-8")
    make_tasks_src = (root / "scripts" / "make_tasks.py").read_text(encoding="utf-8")

    try:
        headings = spec_headings(spec)
    except ValueError as exc:
        return 1, [f"FAIL {exc}"]
    try:
        rows = module_keys(make_tasks_src)
    except ValueError as exc:
        return 1, [f"FAIL {exc}"]

    heading_set = set(headings)
    duplicates = sorted({h for h in headings if headings.count(h) > 1})
    for dup in duplicates:
        lines.append(
            f"FAIL §3.2 heading **{dup}** appears more than once — `test_block` "
            "returns the first and the rest are unreachable"
        )

    reached = {key for _, key in rows if key is not None}

    # Direction 1: a §3.2 block no `M` entry reaches.
    for heading in sorted(heading_set - reached):
        issue = allow.get(heading)
        if issue:
            lines.append(
                f"PASS allowlisted unreached §3.2 block **{heading}** ({issue})"
            )
            continue
        lines.append(
            f"FAIL §3.2 block **{heading}** is reachable from no `M` entry in "
            "scripts/make_tasks.py — its cases reach no task file"
        )

    for name, key in rows:
        if key is None:
            # Direction 2: the task file claims no block exists, and one does.
            for candidate in (f"`{name}`", name):
                if candidate in heading_set:
                    lines.append(
                        f"FAIL `M` entry {name!r} has a None test key, so its task "
                        "file says 'No dedicated test block in §3.2' — but §3.2 "
                        f"has **{candidate}**"
                    )
                    break
            continue
        # Direction 3: a key naming a block that does not exist.
        if key not in heading_set:
            lines.append(
                f"FAIL `M` entry {name!r} names §3.2 block **{key}**, which does "
                "not exist — the task file silently falls back to 'No dedicated "
                "test block in §3.2'"
            )

    # Requirement 4: an allowlist entry that stopped describing a real gap.
    for heading, issue in sorted(allow.items()):
        if heading not in heading_set:
            lines.append(
                f"FAIL stale allowlist entry **{heading}** ({issue}) — §3.2 has "
                "no such block; delete the entry"
            )
        elif heading in reached:
            lines.append(
                f"FAIL stale allowlist entry **{heading}** ({issue}) — an `M` "
                "entry now reaches it; delete the entry"
            )

    failed = [line for line in lines if line.startswith("FAIL")]
    if failed:
        return 1, lines
    lines.append(
        f"PASS test contracts: {len(heading_set)} §3.2 blocks, {len(rows)} `M` "
        f"entries, {len(reached)} keys resolved, "
        f"{len(allow)} allowlisted unreached block(s)"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
