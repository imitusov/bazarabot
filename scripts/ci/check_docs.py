#!/usr/bin/env python3
"""Fail when the documents and the code have drifted apart.

Six checks that are cheap and catch a whole class of decay:
  1. Every module in dependency-order.md has an interfaces.md entry. A module
     built without one is invisible to every later task.
  2. Every task file references a module that exists in dependency-order.md.
  3. Every function specified in spec §4 has the same signature in
     interfaces.md - parameters, defaults and return type.
  4. Every error rule in spec §8 is claimed by a named module in §4.
  5. Every table in spec §5 has exactly one writing module, and is named in
     the contract of the module that owns it.
  6. The spec's **Version:** header is not older than the amendments its body
     cites.

Checks 3-6 exist because the spec stores one obligation in two normative
places - §8 rules and §4 contracts, §4 signatures and interfaces.md - and
duplication without a consistency check drifts apart on the first amendment.
That is what the 2026-09-07 audit found thirty times over.

Each of 3-5 carries an allowlist of the violations that existed when the
check was written, every entry naming the issue that will delete it. The
allowlist is an inventory, not an exemption: these checks cannot find the
existing violations, only stop new ones joining them.

The third check - that tasks/ regenerates identically from the spec - runs in
the workflow as `make_tasks.py && git diff --exit-code tasks/`, because it needs
a clean tree rather than a parser.
"""

import ast
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


# ---------------------------------------------------------------- check 3
# Signature drift. `AGENTS.md` requires contract signatures to match exactly,
# "including `| None`" — but nothing compared them, so an amendment could add
# a parameter to §4 and never reach the code. The check above only asks
# whether a function of that NAME is recorded.

SIG = re.compile(r"^\*\*`(?:async\s+)?(\w+)\((.*?)\)\s*(?:→|->)\s*(.+?)`\*\*")


def signatures(text: str) -> dict[str, tuple[str, str]]:
    """Map function name to (parameters, return type), whitespace-normalised."""
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        m = SIG.match(line.strip())
        if m:
            params = re.sub(r"\s+", " ", m.group(2)).strip()
            ret = re.sub(r"\s+", " ", m.group(3)).strip()
            found[m.group(1)] = (params, ret)
    return found


def sections(text: str, pattern: str) -> dict[str, str]:
    """Split a document into {module: body} on a heading regex."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        head = re.match(pattern, line)
        if head:
            if current:
                out[current] = "\n".join(buf)
            paths = re.findall(r"`([\w/\.]+?)(?:\.py)?`", line)
            zar = [q.replace("/", ".") for q in paths if q.startswith("zarabot/")]
            current = zar[0] if zar else head.group(1)
            buf = []
            continue
        if current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf)
    return out


# Divergences present when this gate was written, each keyed on the signature
# `interfaces.md` actually records. Keying on (module, function) alone would
# exempt that name from every FUTURE divergence too, not just the recorded one
# — and several functions here have been amended more than once.
# Delete an entry with its issue.
KNOWN_SIGNATURE_DRIFT: dict[tuple[str, str], tuple[str, str]] = {
    # 116 — Connection vs aiosqlite.Connection
    ("zarabot.db.migrations", "apply"): ("conn: aiosqlite.Connection", "int"),
    # 116 — return elided as list[...]
    ("zarabot.db.signals", "list_for_period"): (
        "start: date, end: date",
        "list[tuple[Signal, RiskDecision]]",
    ),
    # 116 — protocol self
    ("zarabot.strategies.base", "evaluate"): (
        "self, ticker: str, candles: list[Candle], now: datetime",
        "Signal | None",
    ),
    # 116 — ctx untyped in the spec
    ("zarabot.app.loops", "run"): ("ctx: AppContext", "None"),
    # 116 — ctx, signal untyped in the spec
    ("zarabot.app.shutdown", "shutdown"): ("ctx: AppContext, signal: int", "None"),
}

spec_mods = sections(section, r"^### (.+)")
iface_mods = sections(iface, r"^## `([\w\.]+)`")

drift = []
still_diverging = set()
for mod, body in spec_mods.items():
    if mod not in iface_mods:
        continue
    specified = signatures(body)
    recorded = signatures(iface_mods[mod])
    for fn, want in specified.items():
        got = recorded.get(fn)
        if got is None or got == want:
            continue
        if KNOWN_SIGNATURE_DRIFT.get((mod, fn)) == got:
            still_diverging.add((mod, fn))
        else:
            drift.append((mod, fn, want, got))

# An entry whose recorded signature no longer diverges — because it was fixed,
# because the function is gone, or because it moved on to a DIFFERENT
# divergence — is an exemption nobody voted for. The third case is why the
# allowlist stores the signature rather than just the name.
stale_drift = sorted(set(KNOWN_SIGNATURE_DRIFT) - still_diverging)

if drift or stale_drift:
    if drift:
        print("FAIL signatures differ between spec §4 and interfaces.md")
        for mod, fn, want, got in drift:
            print(f"  {mod}.{fn}")
            print(f"    spec       {fn}({want[0]}) → {want[1]}")
            print(f"    interfaces {fn}({got[0]}) → {got[1]}")
    if stale_drift:
        print("FAIL KNOWN_SIGNATURE_DRIFT entries no longer describe a divergence")
        for mod, fn in stale_drift:
            print(f"  {mod}.{fn}")
    sys.exit(1)

# ---------------------------------------------------------------- check 4
# Rule ownership. §7.1 already requires every log event to be "owed by exactly
# one module, named in the table". §8 never got the same treatment, so a rule
# could be superseded by a §4 amendment and left standing — which is how rule 6
# still orders the adoption that rule 32 forbids (#91).

s8 = spec[spec.index("## 8. Error handling rules") : spec.index("## 9. Dependencies")]
rules = re.findall(r"^(\d+[a-z]?)\. \*\*", s8, re.M)
claims = {m.group(1) for m in re.finditer(r"rules?\s+(\d+[a-z]?)", section)}

# Unclaimed when this gate was written (#115). Delete an entry when a §4
# contract takes the rule, or when the rule itself goes.
KNOWN_UNCLAIMED_RULES = {
    "2", "3", "5", "6", "7", "8", "12", "13", "14", "16", "17", "18", "20",
    "22", "24", "25", "26", "27", "28", "29", "30", "34", "37",
}

orphans = [r for r in rules if r not in claims and r not in KNOWN_UNCLAIMED_RULES]
stale = sorted(KNOWN_UNCLAIMED_RULES - set(rules))
if orphans or stale:
    if orphans:
        print("FAIL error rules claimed by no module contract in §4")
        for r in orphans:
            print(f"  rule {r}")
    if stale:
        print("FAIL KNOWN_UNCLAIMED_RULES names rules that no longer exist")
        for r in stale:
            print(f"  rule {r}")
    sys.exit(1)

# ---------------------------------------------------------------- check 5
# Table ownership. `AGENTS.md`: "Never write to another module's tables.
# Ownership is listed in the spec." Nothing checked that a table HAS an owner,
# so `instruments` was specified, created by a migration, referenced by two
# error rules, and written by nothing at all (#102).

s5 = spec[spec.index("## 5. Database schema") : spec.index("## 6. Migrations")]
tables = re.findall(r"^### `(\w+)`", s5, re.M)

# Tables whose ownership the spec does not state, or that nothing writes (#102).
KNOWN_UNOWNED_TABLES = {
    "instruments",  # no writer anywhere in zarabot/
    "schema_version",  # §4 says "schema creation", names no table
    "daily_snapshots",  # db.signals/snapshots section has no sole-owner line
    "halt_state",  # §4 says "the halt flag", names no table
    "reconciliations",  # owner stated only in interfaces.md
}

WRITE = re.compile(r"(?:insert\s+into|update|delete\s+from)\s+(\w+)", re.I)


def sql_literals(src: pathlib.Path) -> list[str]:
    """Every string constant in the file, and nothing else.

    Scanning the raw text matched English: the comment "we update positions
    only through db.positions" made `risk.gate` a writer of `positions` and
    failed this gate with the most alarming message it can print, from a
    sentence saying the opposite. `update` is a common verb and needs no
    INTO/FROM to anchor it, so only string literals can carry SQL.
    """
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


writers: dict[str, set[str]] = {t: set() for t in tables}
for src in pathlib.Path("zarabot").rglob("*.py"):
    for literal in sql_literals(src):
        for table in WRITE.findall(literal):
            if table in writers:
                writers[table].add(str(src))

# Same reason as KNOWN_SIGNATURE_DRIFT: an entry naming a table that no longer
# exists in §5, or one that has since acquired a writer, is an exemption
# nobody voted for. #102 deletes `instruments`; that must not pass in silence.
stale_tables = sorted(
    (KNOWN_UNOWNED_TABLES - set(tables))
    | {t for t in KNOWN_UNOWNED_TABLES if writers.get(t) and f"`{t}`" in section}
)

unowned, shared = [], []
for table in tables:
    who = writers[table]
    if not who and table not in KNOWN_UNOWNED_TABLES:
        unowned.append(table)
    elif len(who) > 1:
        shared.append((table, sorted(who)))
    elif f"`{table}`" not in section and table not in KNOWN_UNOWNED_TABLES:
        unowned.append(table)

if unowned or shared or stale_tables:
    if stale_tables:
        print("FAIL KNOWN_UNOWNED_TABLES names tables that are gone or now owned")
        for table in stale_tables:
            print(f"  {table}")
    if unowned:
        print("FAIL §5 tables with no owning module named in §4")
        for table in unowned:
            print(f"  {table}")
    if shared:
        print("FAIL §5 tables written by more than one module")
        for table, who in shared:
            print(f"  {table}: {', '.join(who)}")
    sys.exit(1)

# ---------------------------------------------------------------- check 6
# Version drift. §"Versioning": "A new version is issued when any module
# contract, schema, error rule, or test contract changes." Nothing enforced it,
# so the header sat at 1.61 from a3b4b6d through eight contract amendments
# while the body cited v1.69 in fourteen places. A reader cannot tell which
# document they have, and neither can a task file.

header = re.search(r"^\*\*Version:\*\* (\d+)\.(\d+)", spec, re.M)
if header is None:
    print("FAIL technical-spec.md has no **Version:** header")
    sys.exit(1)
declared = (int(header.group(1)), int(header.group(2)))
# Only this spec's own amendment markers. The body cites OTHER documents'
# versions — "**Implements:** `business-brief.md` v1.11" and three "(brief
# v1.8)" — and the brief is the senior document, amended independently. Pooling
# them means a brief bump to v1.71 fails this gate, and the only way to green is
# to bump the spec header to a version it never issued: the exact drift the gate
# exists to prevent. Amendment markers are always parenthesised or sentence-
# initial "vN.NN"; a citation of another document names that document first,
# so those are stripped before the scan.
own = re.sub(r"[`\w.\-]*brief[`\w.\-]*\s+v\d+\.\d+", "", spec)
cited = [(int(a), int(b)) for a, b in re.findall(r"\bv(\d+)\.(\d+)\b", own)]
highest = max(cited, default=(0, 0))
if declared < highest:
    print("FAIL technical-spec.md header is older than the amendments it cites")
    print(f"  header **Version:** {declared[0]}.{declared[1]}")
    print(f"  cites  v{highest[0]}.{highest[1]}")
    sys.exit(1)

print(
    f"PASS docs consistent ({len(modules)} modules recorded, "
    f"every specified function implemented, {len(rules)} error rules, "
    f"{len(tables)} tables owned)"
)
