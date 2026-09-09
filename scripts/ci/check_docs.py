#!/usr/bin/env python3
"""Fail when the documents and the code have drifted apart.

Six checks that are cheap and catch a whole class of decay:
  1. Every module in dependency-order.md has a ``## `module` `` heading in
     interfaces.md (the keys of the parsed sections). A prose mention is not
     an entry. dependency-order.md writes ``db.cooldowns``; the heading is
     ``zarabot.db.cooldowns``. Sandbox modules match the heading as written.
  2. Every function specified in spec §4 is recorded by name in interfaces.md.
     A shared heading is satisfied if any listed module records the name; a
     name that appears in exactly one of those sections is therefore OK for
     "exists". Wrong-module implementation is green. A NEW name recorded only
     under signals cannot be attributed to snapshots without an ownership
     convention in the spec; splitting the heading is the spec-side fix.
     A miss is reported against both modules. Sandbox headings are skipped.
  3. Every function specified in spec §4 has the same signature in
     interfaces.md - parameters, defaults and return type. A heading that
     names two zarabot modules is compared against each module's section.
  4. Every error rule in spec §8 is claimed by a named module in §4.
  5. Every table in spec §5 has exactly one writing module, and is named in
     the contract of the module that owns it.
  6. Each of technical-spec.md, business-brief.md and dependency-order.md has a
     **Version:** header that is not older than that document's own vN.NN
     citations. This is a proxy (citations present and header ≥ max citation),
     not a proof that every contract edit bumped the header. Documents are not
     pooled. Header newer than any citation is allowed. Only technical-spec.md
     carries own-document markers today, so the brief and dependency-order arms
     are dormant (they cannot fail). A gate that reads as covering three
     documents and covers one is failure class 6 unless that limit is written
     down. Do not fail a document that has zero markers.

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
    """Split a document into {module: body} on a heading regex.

    A heading that names more than one zarabot path maps the same body to
    every path, so a signature recorded only under the second module is still
    compared. Skip a function only when it is recorded in none of those
    interfaces.md sections.
    """
    out: dict[str, str] = {}
    current: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not current:
            return
        body = "\n".join(buf)
        for key in current:
            out[key] = body

    for line in text.splitlines():
        head = re.match(pattern, line)
        if head:
            flush()
            paths = re.findall(r"`([\w/\.]+?)(?:\.py)?`", line)
            zar = [q.replace("/", ".") for q in paths if q.startswith("zarabot/")]
            current = zar if zar else [head.group(1)]
            buf = []
            continue
        if current:
            buf.append(line)
    flush()
    return out


# Citations of *another* document name that document first. The brief is senior
# and amended independently; pooling `brief v1.71` into the spec scan would
# force a spec header bump the spec never issued. `technical-spec.md` v1.23 in
# dependency-order.md is the same shape. Do not strip the document under scan:
# `brief v1.12` in business-brief.md and `` `dependency-order.md` v1.5 `` in
# that file are own amendments, not foreign citations (#138 stripped others).
_BRIEF_CITE = r"[`\w.\-]*brief[`\w.\-]*\s+v\d+\.\d+"
_OTHER_DOC_CITE = r"`[\w.\-]+\.md`\s+v\d+\.\d+"


def header_vs_own_citations(
    text: str,
    name: str = "",
) -> tuple[tuple[int, int] | None, tuple[int, int]]:
    """Return (header version, max own citation), each document scanned alone.

    Proxy only: citations present and header ≥ max citation. An amendment with
    no marker is invisible. Header newer than any citation is not a failure.
    `name` is the file being scanned; citations of that file are kept.
    """
    header = re.search(r"^\*\*Version:\*\* (\d+)\.(\d+)", text, re.M)
    declared = (int(header.group(1)), int(header.group(2))) if header else None
    basename = pathlib.Path(name).name if name else ""
    own = text
    if basename != "business-brief.md":
        own = re.sub(_BRIEF_CITE, "", own)
    if basename:
        other = rf"`(?!{re.escape(basename)}`)[\w.\-]+\.md`\s+v\d+\.\d+"
        own = re.sub(other, "", own)
    else:
        own = re.sub(_OTHER_DOC_CITE, "", own)
    cited = [(int(a), int(b)) for a, b in re.findall(r"\bv(\d+)\.(\d+)\b", own)]
    highest = max(cited, default=(0, 0))
    return declared, highest


def version_gate_failures(documents: dict[str, str]) -> list[str]:
    """Run header-vs-own-citations on each document; do not pool markers."""
    lines: list[str] = []
    for name, text in documents.items():
        declared, highest = header_vs_own_citations(text, name)
        if declared is None:
            lines.append(f"FAIL {name} has no **Version:** header")
            continue
        if declared < highest:
            lines.append(
                f"FAIL {name} header is older than the amendments it cites"
            )
            lines.append(f"  header **Version:** {declared[0]}.{declared[1]}")
            lines.append(f"  cites  v{highest[0]}.{highest[1]}")
    return lines


def find_signature_drift(
    spec_section: str,
    iface_text: str,
    known: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[
    list[tuple[str, str, tuple[str, str], tuple[str, str]]],
    set[tuple[str, str]],
]:
    """Compare §4 signatures against interfaces.md. Skip names recorded nowhere."""
    known = known if known is not None else {}
    spec_mods = sections(spec_section, r"^### (.+)")
    iface_mods = sections(iface_text, r"^## `([\w\.]+)`")
    drift: list[tuple[str, str, tuple[str, str], tuple[str, str]]] = []
    still_diverging: set[tuple[str, str]] = set()
    for mod, body in spec_mods.items():
        if mod not in iface_mods:
            continue
        specified = signatures(body)
        recorded = signatures(iface_mods[mod])
        for fn, want in specified.items():
            got = recorded.get(fn)
            if got is None or got == want:
                continue
            if known.get((mod, fn)) == got:
                still_diverging.add((mod, fn))
            else:
                drift.append((mod, fn, want, got))
    return drift, still_diverging


def unimplemented_specified_functions(
    spec_section: str,
    iface_sections: dict[str, str],
) -> list[tuple[str, str]]:
    """§4 names recorded in none of the heading's interfaces.md sections.

    Uses `sections()` for the heading split. Headings with no `zarabot/` path
    (sandbox) are skipped. A shared heading is satisfied if any listed module
    records the name, including when that is exactly one module — wrong-module
    implementation is therefore green. When the name is in none of them,
    report each heading module so the miss is not pinned to current[0].
    """
    spec_mods = sections(spec_section, r"^### (.+)")
    grouped: dict[str, list[str]] = {}
    for mod, body in spec_mods.items():
        if not mod.startswith("zarabot."):
            continue
        grouped.setdefault(body, []).append(mod)
    unimplemented: list[tuple[str, str]] = []
    for body, current in grouped.items():
        seen: set[str] = set()
        for line in body.splitlines():
            fn = re.match(r"^\*\*`(?:async\s+)?(\w+)\(", line)
            if not fn or fn.group(1) in seen:
                continue
            seen.add(fn.group(1))
            wanted = f"{fn.group(1)}("
            if not any(wanted in iface_sections.get(mod, "") for mod in current):
                unimplemented.extend((mod, fn.group(1)) for mod in current)
    return unimplemented


def modules_missing_interface_headings(
    modules: list[str],
    iface: str,
) -> list[str]:
    """Modules with no ``## `...` `` heading in interfaces.md.

    Keys of `sections()` are the headings. Dep-order names without the
    `zarabot.` prefix match `zarabot.{module}`. A substring in prose is not
    a heading.
    """
    keys = set(sections(iface, r"^## `([\w\.]+)`"))
    return [m for m in modules if m not in keys and f"zarabot.{m}" not in keys]


def format_unimplemented(
    unimplemented: list[tuple[str, str]],
    tasks: pathlib.Path,
) -> list[str]:
    """One FAIL line per (module, function), naming that module's task file."""
    lines: list[str] = []
    for mod, fn in unimplemented:
        stem = mod.replace("zarabot.", "").replace(".", "-")
        match = sorted(tasks.glob(f"*-{stem}.md")) if tasks.is_dir() else []
        where = f" — re-run {match[0]}" if match else ""
        lines.append(f"  {mod}.{fn}{where}")
    return lines


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
    # 161 — two-module heading; visible once both keys are compared
    ("zarabot.db.snapshots", "list_for_period"): (
        "start: date, end: date",
        "list[DailySnapshot]",
    ),
    ("zarabot.db.snapshots", "write_daily"): ("snapshot: DailySnapshot", "None"),
}

WRITE = re.compile(r"(?:insert\s+into|update|delete\s+from)\s+(\w+)", re.I)


def main() -> None:
    dep = pathlib.Path("dependency-order.md").read_text(encoding="utf-8")
    iface = pathlib.Path("interfaces.md").read_text(encoding="utf-8")
    modules = re.findall(r"^\d+[a-z]?\. \*\*([\w\.]+)\*\*", dep, re.M)

    iface_sections = sections(iface, r"^## `([\w\.]+)`")
    missing = modules_missing_interface_headings(modules, iface)
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
        # Empty section would skip every signature, rule-claim and table-name
        # check and still print PASS.
        print("FAIL technical-spec.md is missing §4 or §5 headings")
        sys.exit(1)

    # The search is scoped to each module's OWN interfaces.md section (parsed
    # once via `sections()`, same helper as check 1 and check 3). A plain
    # substring search over the whole file passes as soon as any module anywhere
    # records a function of that name, so `config.get` was satisfied by
    # `db.orders.get` and `broker.client.close` by `db.positions.close` — the check
    # could not fail for any common name.
    unimplemented = unimplemented_specified_functions(section, iface_sections)

    if unimplemented:
        print("FAIL functions specified but not recorded in interfaces.md")
        print("\n".join(format_unimplemented(unimplemented, pathlib.Path("tasks"))))
        sys.exit(1)

    # ---------------------------------------------------------------- check 3
    # Signature drift. `AGENTS.md` requires contract signatures to match exactly,
    # "including `| None`" — but nothing compared them, so an amendment could add
    # a parameter to §4 and never reach the code. The check above only asks
    # whether a function of that NAME is recorded.

    drift, still_diverging = find_signature_drift(
        section, iface, known=KNOWN_SIGNATURE_DRIFT
    )

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

    s8 = spec[
        spec.index("## 8. Error handling rules") : spec.index("## 9. Dependencies")
    ]
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
    # Version drift. §"Versioning" claims a new version when a contract, schema,
    # rule or test contract changes. The check is a proxy: header ≥ max own
    # citation. An unmarked amendment is invisible. A git-diff versioner is a
    # different gate and is not this one. Only technical-spec.md carries
    # own-document markers today; the brief and dependency-order arms are
    # dormant. Do not fail a document with zero markers.
    brief = pathlib.Path("business-brief.md").read_text(encoding="utf-8")
    version_fails = version_gate_failures(
        {
            "technical-spec.md": spec,
            "business-brief.md": brief,
            "dependency-order.md": dep,
        }
    )
    if version_fails:
        print("\n".join(version_fails))
        sys.exit(1)

    print(
        f"PASS docs consistent ({len(modules)} modules recorded, "
        f"every specified function implemented, {len(rules)} error rules, "
        f"{len(tables)} tables owned)"
    )


if __name__ == "__main__":
    main()
