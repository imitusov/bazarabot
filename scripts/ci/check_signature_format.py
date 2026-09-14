#!/usr/bin/env python3
"""Fail when a §4 signature is written in a form the drift gate cannot read.

Issue #118 gate 1, second half (#116). `check_docs.py` check 3 compares every
§4 signature against `interfaces.md` and finds them with one regex, `SIG`,
which requires a plain function name, a parenthesised parameter list and
``→`` (or ``->``) before a return type. A §4 signature written any other way
is not reported as *different*; it is never seen, so the gate that reads as
covering §4 covers only the part of §4 that happens to be well formed. That
is failure class 6, in the gate written to close a drift class.

It is not hypothetical. `**`async backtest.run(bars, config, strategies,
commission, slippage) → BacktestResult`**` has a dotted name and five untyped
parameters. `SIG` cannot match it, so nothing has ever compared it with
`interfaces.md`, where `run` takes seven typed parameters including two the
spec line does not mention at all. It is allowlisted below, not fixed:
correcting it is an amendment to technical-spec.md.

Three arms, over every ``**`…`**`` line in §4 that names something callable:

  1. ``→`` or ``->`` followed by a non-empty return type. A signature with no
     stated return is the shape #116 was filed for.
  2. Every parameter is ``name: type``. A default without a type — ``lots =
     1`` — reads as typed and is not. ``*``, ``/``, ``self`` and ``cls`` are
     not parameters and are skipped.
  3. `check_docs.SIG` — imported, not re-implemented — matches the line. This
     is the arm that matters: it asks the drift gate directly whether it can
     see this signature, so the two can never disagree about what §4 contains.

WHAT THIS DOES NOT COVER, stated because a guard about signature readability
must enumerate the readability it does not prove (failure class 6):

  * WHETHER THE SIGNATURE IS RIGHT. Agreement with `interfaces.md` is
    `check_docs.py` check 3's job and agreement with the code is nobody's
    mechanically. This gate only makes a signature legible to check 3; a
    legible signature that is wrong is exactly what check 3 then reports.
  * Signatures that are not standalone bold lines. A signature inside a
    bullet — ``- **`SimulatedExchange(bars: …)`** holds …`` — is invisible
    here, as it is to `check_docs.SIG`, which also anchors at the start of
    the stripped line. The two are blind together by construction rather than
    by accident, which is the point of arm 3.
  * TYPE VALIDITY. ``lots: banana`` passes all three arms. Nothing resolves a
    name in a §4 annotation to anything.
  * Sections other than §4. §3's illustrative signatures and §7's field lists
    are prose and are left alone.
  * Whether a specified function EXISTS. That is check_docs.py checks 1-2.

A green run means every §4 signature is in a form check 3 can compare — NOT
that any of them are correct.

The allowlist is an inventory, each entry naming its issue, and an entry that
stops describing a real defect FAILS rather than being ignored: a stale waiver
is a silent exemption, which is how #124's and #180's entries were caught.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from typing import NamedTuple

# §4 signatures that no gate can read today. Key is the callable name as
# written between the parentheses' left neighbour and the backtick. Every
# entry is a RECORDED DEFECT in technical-spec.md, waived only because this
# gate may not amend the spec. Do not add an entry to make the gate pass.
KNOWN_UNREADABLE_SIGNATURES: dict[str, str] = {
    "backtest.run": (
        "#116/#118 — dotted name and five untyped parameters, so check_docs.py "
        "check 3 has never compared it; interfaces.md records `run` with seven "
        "typed parameters, two of which (`instruments`, `reject_stops`) the §4 "
        "line does not mention. Fixing it is a spec amendment"
    ),
}

_H4 = re.compile(r"(?m)^## 4\.[ ]")
_NEXT_H2 = re.compile(r"(?m)^## ")
_BOLD_CODE = re.compile(r"^\*\*`(.+?)`\*\*")
_NAME = re.compile(r"^(?:async\s+)?([A-Za-z_][\w.]*)\(")
_ARROW = re.compile(r"^\s*(?:→|->)\s*(.*)$")

_CHECK_DOCS = pathlib.Path(__file__).with_name("check_docs.py")


class InputError(ValueError):
    """An input moved — reported as a FAIL line, never as a traceback."""


class Signature(NamedTuple):
    """One §4 signature line, split into the pieces the three arms read."""

    line_no: int
    name: str
    params: str
    tail: str
    raw: str


def _drift_gate_regex() -> re.Pattern[str]:
    """`check_docs.SIG`, imported rather than copied.

    A copy would drift from the gate whose blindness this one exists to
    measure, which is the failure being checked for one level up.
    """
    spec = importlib.util.spec_from_file_location("check_docs", _CHECK_DOCS)
    if spec is None or spec.loader is None:
        raise InputError(f"cannot load {_CHECK_DOCS.as_posix()}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_docs", module)
    spec.loader.exec_module(module)
    pattern = getattr(module, "SIG", None)
    if not isinstance(pattern, re.Pattern):
        raise InputError(
            "scripts/ci/check_docs.py has no module-level `SIG` pattern — the "
            "drift gate's own signature parser has moved or been renamed"
        )
    return pattern


def _split_params(params: str) -> list[str]:
    """Top-level comma split, ignoring commas inside brackets."""
    depth = 0
    current = ""
    parts: list[str] = []
    for char in params:
        if char in "[({":
            depth += 1
        elif char in "])}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def section_4_signatures(spec: str) -> list[Signature]:
    """Every callable-shaped ``**`…`**`` line inside §4.

    Raises InputError when the anchor has moved or the section holds none. A
    moved anchor must be a failure and never an empty result: two gates in
    this repo shipped that defect (#165, #163) and passed with zero rows.
    """
    lines = spec.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _H4.match(line):
            start = i
            break
    if start is None:
        raise InputError(
            "technical-spec.md has no '## 4. ' heading — §4 was renamed or "
            "renumbered, so its signatures are no longer gated"
        )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _NEXT_H2.match(lines[i]):
            end = i
            break
    found: list[Signature] = []
    for i in range(start, end):
        raw = lines[i].strip()
        hit = _BOLD_CODE.match(raw)
        if hit is None:
            continue
        inner = hit.group(1)
        name_hit = _NAME.match(inner)
        if name_hit is None:
            continue  # ``**`PriceRejected`**`` — a name, not a callable
        depth = 0
        close = None
        for pos in range(name_hit.end() - 1, len(inner)):
            if inner[pos] == "(":
                depth += 1
            elif inner[pos] == ")":
                depth -= 1
                if depth == 0:
                    close = pos
                    break
        if close is None:
            continue  # unbalanced: not a signature this gate can claim
        found.append(
            Signature(
                line_no=i + 1,
                name=name_hit.group(1),
                params=inner[name_hit.end() : close],
                tail=inner[close + 1 :],
                raw=raw,
            )
        )
    if not found:
        raise InputError(
            "technical-spec.md §4 contains no signature lines — a parse that "
            "finds nothing is a broken parse, not a clean tree"
        )
    return found


def defects(signature: Signature, drift_sig: re.Pattern[str]) -> list[str]:
    """Every reason this signature is unreadable, as message fragments."""
    out: list[str] = []
    arrow = _ARROW.match(signature.tail)
    if arrow is None:
        out.append(
            "carries no → (or ->) before a return type, so check_docs.py's "
            "SIG skips it and nothing compares it with interfaces.md"
        )
    elif not arrow.group(1).strip():
        out.append("has → with an empty return type")
    for part in _split_params(signature.params):
        if part in ("*", "/", "self", "cls"):
            continue
        before_default = part.split("=", 1)[0]
        if ":" not in before_default:
            out.append(
                f"parameter {part!r} has no `name: type` annotation — a "
                "default without a type reads as typed and is not"
            )
    if not drift_sig.match(signature.raw):
        out.append(
            "is not matched by check_docs.py's own SIG pattern, so the "
            "signature-diff gate cannot see it at all"
        )
    return out


def evaluate(
    root: pathlib.Path,
    *,
    known: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    allow = KNOWN_UNREADABLE_SIGNATURES if known is None else known
    lines: list[str] = []
    spec = (root / "technical-spec.md").read_text(encoding="utf-8")

    try:
        drift_sig = _drift_gate_regex()
        found = section_4_signatures(spec)
    except InputError as exc:
        return 1, [f"FAIL {exc}"]

    # Names, not lines, because the allowlist is keyed by name and one name
    # can be specified in two module sections (`run`, `send`). A name counts
    # as unreadable when ANY of its lines is, so waiving one instance can
    # never quietly waive a second.
    unreadable: set[str] = set()
    seen: set[str] = set()
    readable_lines = 0
    for signature in found:
        seen.add(signature.name)
        problems = defects(signature, drift_sig)
        if not problems:
            readable_lines += 1
            continue
        unreadable.add(signature.name)
        issue = allow.get(signature.name)
        if issue:
            lines.append(
                f"PASS allowlisted unreadable §4 signature `{signature.name}` "
                f"at line {signature.line_no} ({issue})"
            )
            continue
        for problem in problems:
            lines.append(
                f"FAIL technical-spec.md:{signature.line_no} §4 signature "
                f"`{signature.name}` {problem}"
            )

    for name, issue in sorted(allow.items()):
        if name not in seen:
            lines.append(
                f"FAIL stale allowlist entry `{name}` ({issue}) — §4 has no such "
                "signature; delete the entry"
            )
        elif name not in unreadable:
            lines.append(
                f"FAIL stale allowlist entry `{name}` ({issue}) — the signature "
                "is readable now; delete the entry"
            )

    failed = [line for line in lines if line.startswith("FAIL")]
    if failed:
        return 1, lines
    lines.append(
        f"PASS §4 signature format: {len(found)} signature line(s) over "
        f"{len(seen)} name(s), {readable_lines} readable, "
        f"{len(allow)} allowlisted name(s)"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
