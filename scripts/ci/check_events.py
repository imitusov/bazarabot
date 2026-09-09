#!/usr/bin/env python3
"""Fail when a §7.1 log event is not emitted from its owning module.

Issue #118 item 6 (the code half of #54): each catalogue row names one owner
and a list of fields. The owner must put the event literal in `extra={...}`
(or the equivalent `extra={"event": event, **fields}` helper) with every
required field as a key on that emit.

WHAT THIS DOES NOT COVER, stated because a guard described as proving
completeness must enumerate what it leaves out (failure class 6):

  * Exclusivity. §7.1 says in bold "Each event is owed by exactly one module,
    named in the table (v1.60)", and this checks only that the owner emits it.
    A second module emitting the same name passes.
  * Fields beyond §7.1. An emit carrying keys the table does not list passes.
  * Orphan producers. A name emitted anywhere and absent from §7.1 passes.
    `export_health.py` catches this one at runtime by exiting non-zero on an
    unknown name; nothing catches it at review time.
  * The Level column. It is parsed and discarded — §7.1's prose obligation is
    about fields, so a row's INFO/ERROR/CRITICAL is not enforced. Emitting
    `order_rejected` at WARNING passes.

The first three need a tree-wide walk this design never does; the fourth is a
few lines once one exists. So a green run here means "every §7.1 event is
emitted by its owner with the required fields", not "§7.1 is enforced".

The allowlist is an inventory of gaps that existed when the gate was written.
Every entry names the issue that will delete it. A silent skip would hide
new missing events behind yesterday's holes.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from dataclasses import dataclass

# Gaps present when this gate was written. Key is (event, field-or-None).
# None as the field means the event itself is missing from the owner.
# Delete an entry with its issue — a stale row is a silent exemption.
# Populated after scanning today's tree; an empty inventory is still an
# inventory (the PASS line prints "allowlisted gaps: none").
KNOWN_EVENT_GAPS: dict[tuple[str, str | None], str] = {}


@dataclass(frozen=True)
class EventRow:
    name: str
    owner: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


_FIELD = re.compile(r"`(\w+)`(\s*\(only [^)]+\))?")
_EVENT_CELL = re.compile(r"`([^`]+)`")

_COMPOUND: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
) + ((ast.Match,) if hasattr(ast, "Match") else ())


def parse_event_rows(spec: str) -> list[EventRow]:
    """Parse the §7.1 markdown table into one row per event name."""
    try:
        start = spec.index("### 7.1")
        end = spec.index("## 8.", start)
    except ValueError:
        return []
    section = spec[start:end]
    rows: list[EventRow] = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        event_cell, owner_cell, _level, fields_cell = cells[:4]
        owners = _EVENT_CELL.findall(owner_cell)
        if not owners:
            continue
        owner = owners[0]
        names = [n.strip() for n in _EVENT_CELL.findall(event_cell)]
        field_groups = [g.strip() for g in fields_cell.split(" / ")]
        if len(field_groups) == 1:
            field_groups = field_groups * len(names)
        elif len(field_groups) != len(names):
            field_groups = [fields_cell] * len(names)
        for name, group in zip(names, field_groups, strict=False):
            required: list[str] = []
            optional: list[str] = []
            for match in _FIELD.finditer(group):
                field = match.group(1)
                if match.group(2):
                    optional.append(field)
                else:
                    required.append(field)
            rows.append(
                EventRow(
                    name=name,
                    owner=owner,
                    required=tuple(required),
                    optional=tuple(optional),
                )
            )
    return rows


def owner_path(root: pathlib.Path, owner: str) -> pathlib.Path:
    return root.joinpath(*owner.split(".")).with_suffix(".py")


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _call_func_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dict_keys_and_event(
    node: ast.AST,
    assigned: dict[str, object],
) -> tuple[set[str], set[str]]:
    """Return (field keys, event names) for a dict expression.

    Event names come from a Constant `"event"` value, or from a Name that
    this function assigned a string to (possibly in more than one branch).
    """
    keys: set[str] = set()
    events: set[str] = set()
    if isinstance(node, ast.Name) and node.id in assigned:
        return _dict_keys_and_event(assigned[node.id], assigned)  # type: ignore[arg-type]
    if not isinstance(node, ast.Dict):
        return keys, events
    for key_node, value in zip(node.keys, node.values, strict=True):
        if key_node is None:
            # **fields
            if isinstance(value, ast.Name) and value.id in assigned:
                nested_keys, nested_events = _dict_keys_and_event(
                    assigned[value.id], assigned
                )
                keys |= nested_keys
                events |= nested_events
            continue
        key = _const_str(key_node)
        if key is None:
            continue
        if key == "event":
            literal = _const_str(value)
            if literal is not None:
                events.add(literal)
            elif isinstance(value, ast.Name):
                bound = assigned.get(value.id)
                if isinstance(bound, str):
                    events.add(bound)
                elif isinstance(bound, set):
                    events |= {x for x in bound if isinstance(x, str)}
            continue
        keys.add(key)
    return keys, events


def _record_assign(
    assigned: dict[str, object], target: ast.AST, value: ast.AST
) -> None:
    if not isinstance(target, ast.Name):
        return
    literal = _const_str(value)
    if literal is not None:
        existing = assigned.get(target.id)
        if isinstance(existing, set):
            existing.add(literal)
        elif isinstance(existing, str):
            assigned[target.id] = {existing, literal}
        else:
            assigned[target.id] = literal
        return
    if isinstance(value, ast.Dict):
        assigned[target.id] = value
        return
    if isinstance(value, ast.Name) and value.id in assigned:
        assigned[target.id] = assigned[value.id]


def _merge_assigned(into: dict[str, object], src: dict[str, object]) -> None:
    for key, value in src.items():
        if key not in into:
            into[key] = value
            continue
        current = into[key]
        if isinstance(value, str) and isinstance(current, str) and value != current:
            into[key] = {current, value}
        elif isinstance(value, str) and isinstance(current, set):
            current.add(value)
        elif isinstance(value, set) and isinstance(current, str):
            into[key] = value | {current}
        elif isinstance(value, set) and isinstance(current, set):
            current |= value


def _record_subscript(
    assigned: dict[str, object], target: ast.AST, extra_keys: dict[str, set[str]]
) -> None:
    if not isinstance(target, ast.Subscript):
        return
    name = _name(target.value)
    key = _const_str(target.slice)
    if name is None or key is None:
        return
    extra_keys.setdefault(name, set()).add(key)
    stored = assigned.get(name)
    if isinstance(stored, ast.Dict):
        stored.keys.append(ast.Constant(key))
        stored.values.append(ast.Constant(None))


def _is_extra_event_helper(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the function logs `extra={"event": event, **fields}`."""
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    if "event" not in params:
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                continue
            for key_node, value in zip(kw.value.keys, kw.value.values, strict=True):
                if (
                    key_node is not None
                    and _const_str(key_node) == "event"
                    and _name(value) == "event"
                ):
                    return True
    return False


def _compound_headers(stmt: ast.stmt) -> list[ast.AST]:
    """Expression nodes in a compound statement that are not its body."""
    if isinstance(stmt, ast.If | ast.While):
        return [stmt.test]
    if isinstance(stmt, ast.For | ast.AsyncFor):
        return [stmt.target, stmt.iter]
    if isinstance(stmt, ast.With | ast.AsyncWith):
        return list(stmt.items)
    if isinstance(stmt, ast.Try):
        return [h.type for h in stmt.handlers if h.type is not None]
    if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
        nodes: list[ast.AST] = [stmt.subject]
        for case in stmt.cases:
            nodes.append(case.pattern)
            if case.guard is not None:
                nodes.append(case.guard)
        return nodes
    return []


def collect_emits(source: str) -> dict[str, list[set[str]]]:
    """Map event name to a list of key-sets, one per emit site in this file."""
    tree = ast.parse(source)
    helpers: set[str] = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        and _is_extra_event_helper(fn)
    }
    emits: dict[str, list[set[str]]] = {}

    def add(event: str, keys: set[str]) -> None:
        emits.setdefault(event, []).append(set(keys))

    def visit_calls(
        root: ast.AST,
        assigned: dict[str, object],
        extra_keys: dict[str, set[str]],
    ) -> None:
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            func_name = _call_func_name(node.func)
            if func_name in helpers:
                event_lit: str | None = None
                for arg in node.args:
                    lit = _const_str(arg)
                    if lit is not None:
                        event_lit = lit
                keys: set[str] = set()
                for kw in node.keywords:
                    if kw.arg is None:
                        nested, _ = _dict_keys_and_event(kw.value, assigned)
                        if isinstance(kw.value, ast.Name):
                            keys |= extra_keys.get(kw.value.id, set())
                        keys |= nested
                    elif kw.arg != "event":
                        keys.add(kw.arg)
                if event_lit is not None:
                    add(event_lit, keys)
                continue
            for kw in node.keywords:
                if kw.arg != "extra":
                    continue
                keys, events = _dict_keys_and_event(kw.value, assigned)
                if isinstance(kw.value, ast.Name):
                    keys |= extra_keys.get(kw.value.id, set())
                for event in events:
                    add(event, keys)

    def visit_block(body: list[ast.stmt], assigned: dict[str, object]) -> None:
        extra_keys: dict[str, set[str]] = {}
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                _record_assign(assigned, stmt.targets[0], stmt.value)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                _record_assign(assigned, stmt.target, stmt.value)
            elif isinstance(stmt, ast.AugAssign):
                pass
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                _record_subscript(assigned, stmt.targets[0], extra_keys)
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                # Copy the enclosing map so closures and module-level dicts
                # remain visible; inner assigns must not leak outward.
                visit_block(stmt.body, dict(assigned))
                continue
            # Compound bodies are owned by visit_block recursion. Walking the
            # whole statement would record a phantom site with the outer map.
            # Headers (`for x in _emit(...)`, `with _emit()`, `if _emit()`)
            # are not in the body and would otherwise be invisible.
            if isinstance(stmt, _COMPOUND):
                for header in _compound_headers(stmt):
                    visit_calls(header, assigned, extra_keys)
            else:
                visit_calls(stmt, assigned, extra_keys)

            if isinstance(stmt, ast.If):
                then_a = dict(assigned)
                else_a = dict(assigned)
                visit_block(stmt.body, then_a)
                visit_block(stmt.orelse, else_a)
                _merge_assigned(assigned, then_a)
                _merge_assigned(assigned, else_a)
            elif isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
                visit_block(stmt.body, dict(assigned))
                visit_block(stmt.orelse, dict(assigned))
            elif isinstance(stmt, ast.With | ast.AsyncWith):
                visit_block(stmt.body, dict(assigned))
            elif isinstance(stmt, ast.Try):
                visit_block(stmt.body, dict(assigned))
                for handler in stmt.handlers:
                    visit_block(handler.body, dict(assigned))
                visit_block(stmt.orelse, dict(assigned))
                visit_block(stmt.finalbody, dict(assigned))
            elif hasattr(ast, "Match") and isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    visit_block(case.body, dict(assigned))

    visit_block(tree.body, {})
    return emits


def evaluate(
    spec_path: pathlib.Path,
    zarabot_root: pathlib.Path,
    allowlist: dict[tuple[str, str | None], str],
) -> tuple[int, list[str]]:
    """Check §7.1 rows against owner modules. Return (exit code, lines)."""
    spec = spec_path.read_text(encoding="utf-8")
    rows = parse_event_rows(spec)
    if not rows:
        return (
            1,
            ["FAIL spec §7.1 event table not found — the slice above moved"],
        )
    missing: list[str] = []
    field_gaps: list[str] = []
    inventory: list[str] = []
    still: set[tuple[str, str | None]] = set()

    for row in rows:
        path = owner_path(zarabot_root, row.owner)
        if not path.is_file():
            key = (row.name, None)
            if key in allowlist:
                still.add(key)
                inventory.append(
                    f"  {row.name} (owner {row.owner} missing): {allowlist[key]}"
                )
            else:
                missing.append(f"  {row.name}: owner {row.owner} has no file {path}")
            continue
        sites = collect_emits(path.read_text(encoding="utf-8")).get(row.name, [])
        if not sites:
            key = (row.name, None)
            if key in allowlist:
                still.add(key)
                inventory.append(f"  {row.name} not emitted: {allowlist[key]}")
            else:
                missing.append(
                    f"  {row.name}: not emitted in extra={{...}} by {row.owner}"
                )
            continue
        for keys in sites:
            for field in row.required:
                if field in keys:
                    continue
                key = (row.name, field)
                if key in allowlist:
                    still.add(key)
                    inventory.append(
                        f"  {row.name}.{field} missing in {row.owner}: {allowlist[key]}"
                    )
                else:
                    field_gaps.append(
                        f"  {row.name}: extra emit in {row.owner} missing key {field}"
                    )

    stale = sorted(set(allowlist) - still)
    lines: list[str] = []
    failed = bool(missing or field_gaps or stale)
    if missing or field_gaps:
        lines.append("FAIL §7.1 events missing from the owning module extra=")
        lines.extend(missing)
        lines.extend(field_gaps)
    if stale:
        lines.append("FAIL KNOWN_EVENT_GAPS entries are stale")
        for event, field in stale:
            label = event if field is None else f"{event}.{field}"
            lines.append(f"  {label}")
    if not failed:
        n = len(rows)
        if inventory:
            unique = sorted(set(inventory))
            lines.append(
                f"PASS §7.1 event emission ({n} events; "
                f"{len(still)} allowlisted gaps — inventory, not a skip):"
            )
            lines.extend(unique)
        else:
            lines.append(
                f"PASS §7.1 event emission ({n} events; allowlisted gaps: none)"
            )
    return (1 if failed else 0, lines)


def main() -> int:
    root = pathlib.Path(".")
    code, lines = evaluate(
        root / "technical-spec.md",
        root / "zarabot",
        KNOWN_EVENT_GAPS,
    )
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
