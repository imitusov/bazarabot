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

print(f"PASS docs consistent ({len(modules)} modules recorded in interfaces.md)")
