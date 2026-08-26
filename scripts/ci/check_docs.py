#!/usr/bin/env python3
"""Fail when the documents and the code have drifted apart.

Two checks that are cheap and catch a whole class of decay:
  1. Every module in dependency-order.md has an interfaces.md entry. A module
     built without one is invisible to every later task.
  2. Every task file references a module that exists in dependency-order.md.

The third check - that tasks/ regenerates identically from the spec - runs in
the workflow as `make_tasks.py && git diff --exit-code tasks/`, because it needs
a clean tree rather than a parser.
"""

import pathlib
import re
import sys

dep = pathlib.Path("dependency-order.md").read_text(encoding="utf-8")
iface = pathlib.Path("interfaces.md").read_text(encoding="utf-8")
modules = re.findall(r"^\d+[a-z]?\. \*\*([\w\.]+)\*\*", dep, re.M)

missing = [m for m in modules if m not in iface]
if missing:
    print("FAIL modules missing from interfaces.md")
    for m in missing:
        print(f"  {m}")
    sys.exit(1)

# Function-level drift. A spec amendment that adds a function to a module and
# is not followed by re-running that module's task leaves the spec specifying
# something no code provides. The next agent to need it is blocked mid-task and
# must either guess it, reimplement it in the wrong module, or stop - and only
# the third is correct. Catching it here turns that into a failed check
# immediately after the amendment, naming the task to re-run.
spec = pathlib.Path("technical-spec.md").read_text(encoding="utf-8")
try:
    start = spec.index("## 4. Module contracts")
    end = spec.index("## 5. Database schema")
    section = spec[start:end]
except ValueError:
    section = ""

# The search is scoped to the module's OWN section of interfaces.md. A plain
# substring search over the whole file passes as soon as any module anywhere
# records a function of that name, so `config.get` was satisfied by
# `db.orders.get` and `broker.client.close` by `db.positions.close` — the check
# could not fail for any common name.
iface_sections: dict[str, str] = {}
current_iface = None
for line in iface.splitlines():
    head = re.match(r"^## `([\w\.]+)`", line)
    if head:
        current_iface = head.group(1)
        iface_sections[current_iface] = ""
        continue
    if current_iface:
        iface_sections[current_iface] += line + "\n"

# A heading may name more than one module — `### `a.py`, `b.py`` — and the
# function may be recorded under either. `sandbox/` is skipped: it is research
# code that zarabot never imports and it has no interfaces.md section.
current: list[str] = []
unimplemented: list[tuple[str, str]] = []
for line in section.splitlines():
    if line.startswith("### "):
        paths = re.findall(r"`([\w/\.]+?)(?:\.py)?`", line)
        current = [
            p.replace("/", ".") for p in paths if p.startswith("zarabot/")
        ]
        continue
    fn = re.match(r"^\*\*`(?:async\s+)?(\w+)\(", line)
    if fn and current:
        wanted = f"{fn.group(1)}("
        if not any(wanted in iface_sections.get(mod, "") for mod in current):
            unimplemented.append((current[0], fn.group(1)))

if unimplemented:
    print("FAIL functions specified but not recorded in interfaces.md")
    tasks = pathlib.Path("tasks")
    for mod, fn in unimplemented:
        stem = mod.replace("zarabot.", "").replace(".", "-")
        match = sorted(tasks.glob(f"*-{stem}.md")) if tasks.is_dir() else []
        where = f" — re-run {match[0]}" if match else ""
        print(f"  {mod}.{fn}{where}")
    sys.exit(1)

print(
    f"PASS docs consistent ({len(modules)} modules recorded, "
    "every specified function implemented)"
)
