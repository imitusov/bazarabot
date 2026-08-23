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

current = None
unimplemented: list[tuple[str, str]] = []
for line in section.splitlines():
    heading = re.match(r"^### `([\w/\.]+?)(?:\.py)?`", line)
    if heading:
        current = heading.group(1).replace("/", ".")
        continue
    fn = re.match(r"^\*\*`(?:async\s+)?(\w+)\(", line)
    if fn and current and f"{fn.group(1)}(" not in iface:
        unimplemented.append((current, fn.group(1)))

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
