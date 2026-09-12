#!/usr/bin/env python3
"""Fail when AGENTS.md drifts from §8, the tree sketch, or coverage floors.

Issue #118 S-28 item 7 (rulebook consistency). The issue asked to match
``find zarabot sandbox -name '*.py'``. That check would have redded main when
this gate was written, because the File structure block was then a package
sketch rather than an inventory — ``sandbox/`` listed data, backtest and train
but not ``exchange.py``, and ``zarabot/`` omitted ``__main__.py``. #117 item 9
wrote every module into the sketch and `check_file_tree.py` now enforces the
match in both directions. This gate therefore:

  * parses the highest integer ordinal in technical-spec.md §8 and the
    ``rules (1–N)`` range in AGENTS.md; ``9b`` is an extra label, not a new
    high. CLAUDE.md must be a symlink to AGENTS.md so the range is not
    parsed twice.
  * requires every directory named in that sketch to exist, and fails on a
    new top-level package under ``zarabot/`` that the sketch does not name
    (allowlist: ``KNOWN_EXTRA_ZARABOT_PACKAGES``).
  * requires the Coverage sentence's 80 / 70 / 95 and the four money-path
    modules to match ``DEFAULT`` / ``RELAXED`` / ``STRICT`` and
    ``STRICT_MODULES`` in ``scripts/ci/check_coverage.py``.

WHAT THIS DOES NOT COVER:

  * Every ``.py`` file appearing in the sketch. Nested names (``models``,
    ``client``, ``train``) are documentation, not a file inventory.
  * ``find zarabot sandbox -name '*.py'`` completeness (#117 as written) —
    that is `check_file_tree.py`'s job, not this one's.
  * §8 rule *text* vs the Must NEVER / Must ALWAYS bullets.
  * Lettered ordinals other than ``9b`` — a new ``Nb`` fails rather than
    being folded into the integer range.
  * CLAUDE.md body when it is a symlink (identity is the link target).
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# Gaps present when this gate was written. Delete an entry with its issue.
KNOWN_EXTRA_ZARABOT_PACKAGES: dict[str, str] = {}
KNOWN_MISSING_SKETCH_PATHS: dict[str, str] = {}

_SECTION_8 = re.compile(r"(?m)^## 8\. Error handling rules\s*$")
_NEXT_H2 = re.compile(r"(?m)^## ")
_ORDINAL = re.compile(r"(?m)^(\d+)(b)?\. ")
_RANGE = re.compile(
    r"numbered error rules \(1[–-](\d+)(?:, plus 9b)?\)",
)
_RANGE_9B = re.compile(
    r"numbered error rules \(1[–-](\d+), plus 9b\)",
)
_FILE_STRUCTURE = re.compile(
    r"(?ms)^## File structure\s*\n+```[^\n]*\n(.*?)```",
)
_SKETCH_DIR = re.compile(r"^(\s*)([A-Za-z0-9_./]+)/\s*")
_COVERAGE_BLOCK = re.compile(
    r"\*\*Coverage\.\*\*(.+?)(?:\n\n|\n## )",
    re.S,
)
_OVERALL = re.compile(r"(\d+(?:\.\d+)?)%\s+overall")
_APP = re.compile(r"(\d+(?:\.\d+)?)%\s+for\s+`app\.\*`")
_STRICT_PCT = re.compile(r"\*\*(\d+(?:\.\d+)?)%\s+for")
_MODULE = re.compile(r"`([a-z][\w]*\.[a-z][\w]*)`")


def section_8(spec: str) -> str:
    hit = _SECTION_8.search(spec)
    if hit is None:
        raise ValueError("technical-spec.md has no '## 8. Error handling rules'")
    rest = spec[hit.end() :]
    nxt = _NEXT_H2.search(rest)
    return rest[: nxt.start()] if nxt else rest


def spec_ordinals(spec: str) -> tuple[int, bool, frozenset[str]]:
    body = section_8(spec)
    ints: list[int] = []
    lettered: set[str] = set()
    for num, bee in _ORDINAL.findall(body):
        if bee:
            lettered.add(f"{num}b")
            continue
        ints.append(int(num))
    if not ints:
        raise ValueError("§8 contains no numbered rules")
    return max(ints), "9b" in lettered, frozenset(lettered)


def agents_range(agents: str) -> tuple[int, bool]:
    hits = list(_RANGE.finditer(agents))
    if len(hits) != 1:
        raise ValueError(
            f"AGENTS.md must contain exactly one 'numbered error rules (1–N)' "
            f"clause, found {len(hits)}"
        )
    high = int(hits[0].group(1))
    has_9b = _RANGE_9B.search(agents) is not None
    return high, has_9b


def sketch_directories(agents: str) -> list[str]:
    hit = _FILE_STRUCTURE.search(agents)
    if hit is None:
        raise ValueError("AGENTS.md has no File structure fenced sketch")
    paths: list[str] = []
    for line in hit.group(1).splitlines():
        m = _SKETCH_DIR.match(line)
        if m is None:
            continue
        indent, name = m.group(1), m.group(2).rstrip("/")
        if indent:
            paths.append(f"zarabot/{name}")
        else:
            paths.append(name)
    if not paths:
        raise ValueError("File structure sketch names no directories")
    return paths


def coverage_constants(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    wanted = {
        "STRICT",
        "RELAXED",
        "DEFAULT",
        "STRICT_MODULES",
        "RELAXED_PREFIXES",
    }
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in wanted:
            continue
        found[target.id] = ast.literal_eval(node.value)
    missing = wanted - found.keys()
    if missing:
        raise ValueError(f"check_coverage.py missing {sorted(missing)}")
    return found


def agents_coverage(agents: str) -> tuple[float, float, float, set[str]]:
    hit = _COVERAGE_BLOCK.search(agents)
    if hit is None:
        raise ValueError("AGENTS.md has no Coverage paragraph")
    para = hit.group(1)
    overall = _OVERALL.search(para)
    app = _APP.search(para)
    strict = _STRICT_PCT.search(para)
    if overall is None or app is None or strict is None:
        raise ValueError("AGENTS.md Coverage sentence is missing a percentage")
    modules = set(_MODULE.findall(para))
    return (
        float(overall.group(1)),
        float(app.group(1)),
        float(strict.group(1)),
        modules,
    )


def _module_to_path(name: str) -> str:
    return f"zarabot/{name.replace('.', '/')}.py"


def _check_claude(root: pathlib.Path, lines: list[str]) -> None:
    claude = root / "CLAUDE.md"
    if not claude.exists():
        lines.append("FAIL CLAUDE.md is missing (must be a symlink to AGENTS.md)")
        return
    if not claude.is_symlink():
        lines.append(
            "FAIL CLAUDE.md must be a symlink to AGENTS.md (not a second copy)"
        )
        return
    target = claude.readlink()
    if pathlib.Path(target).name != "AGENTS.md":
        lines.append(f"FAIL CLAUDE.md symlink target is {target!r}, expected AGENTS.md")


def _check_rules(spec: str, agents: str, lines: list[str]) -> None:
    # Wrapped like `agents_range` below: a renamed §8 heading is a FAIL line,
    # not a traceback. Every other failure here reports through `lines`, and a
    # gate that exits on a stack trace when its input moves reads as a crash
    # rather than as the finding it is.
    try:
        high, spec_9b, lettered = spec_ordinals(spec)
    except ValueError as exc:
        lines.append(f"FAIL {exc}")
        return
    extra = lettered - {"9b"}
    if extra:
        lines.append(
            f"FAIL §8 has lettered rules {sorted(extra)} — only 9b is recognised"
        )
    try:
        agents_high, agents_9b = agents_range(agents)
    except ValueError as exc:
        lines.append(f"FAIL {exc}")
        return
    if agents_high != high:
        lines.append(
            f"FAIL AGENTS.md rule range 1–{agents_high} != §8 highest ordinal {high}"
        )
    if spec_9b != agents_9b:
        if spec_9b:
            lines.append("FAIL §8 has 9b but AGENTS.md range omits 'plus 9b'")
        else:
            lines.append("FAIL AGENTS.md claims 'plus 9b' but §8 has no 9b")


def _check_tree(
    root: pathlib.Path,
    agents: str,
    lines: list[str],
    extra_packages: dict[str, str],
    missing_paths: dict[str, str],
) -> None:
    try:
        named = sketch_directories(agents)
    except ValueError as exc:
        lines.append(f"FAIL {exc}")
        return
    allowlisted_missing: list[str] = []
    for rel in named:
        path = root / rel
        if path.is_dir():
            continue
        issue = missing_paths.get(rel)
        if issue:
            allowlisted_missing.append(f"{rel} ({issue})")
            continue
        lines.append(f"FAIL sketch names {rel}/ but that path does not exist")
    if allowlisted_missing:
        lines.append(
            "PASS allowlisted missing sketch paths: " + ", ".join(allowlisted_missing)
        )

    named_children = {
        p.split("/", 1)[1] for p in named if p.startswith("zarabot/") and "/" in p
    }
    zarabot = root / "zarabot"
    if not zarabot.is_dir():
        lines.append("FAIL zarabot/ is missing")
        return
    actual = {
        p.name
        for p in zarabot.iterdir()
        if p.is_dir() and p.name != "__pycache__" and not p.name.startswith(".")
    }
    unexpected = actual - named_children
    allowlisted_extra: list[str] = []
    for name in sorted(unexpected):
        issue = extra_packages.get(name)
        if issue:
            allowlisted_extra.append(f"{name} ({issue})")
            continue
        lines.append(
            f"FAIL zarabot/{name}/ is not named in the AGENTS.md File structure sketch"
        )
    if allowlisted_extra:
        lines.append(
            "PASS allowlisted extra zarabot packages: " + ", ".join(allowlisted_extra)
        )


_FAIL_UNDER = re.compile(r"(?m)^fail_under\s*=\s*(\d+(?:\.\d+)?)")


def _fail_under(pyproject: str) -> float | None:
    """coverage's overall gate, from pyproject.toml.

    The rulebook's "N% overall" is enforced here, not by check_coverage's
    per-module DEFAULT. Two numbers, one sentence in AGENTS.md.
    """
    hit = _FAIL_UNDER.search(pyproject)
    return float(hit.group(1)) if hit else None


def _check_coverage(
    agents: str, coverage_src: str, pyproject: str, lines: list[str]
) -> None:
    try:
        consts = coverage_constants(coverage_src)
        overall, relaxed, strict, modules = agents_coverage(agents)
    except ValueError as exc:
        lines.append(f"FAIL {exc}")
        return
    default = float(consts["DEFAULT"])  # type: ignore[arg-type]
    relaxed_c = float(consts["RELAXED"])  # type: ignore[arg-type]
    strict_c = float(consts["STRICT"])  # type: ignore[arg-type]
    # "80% overall" is enforced by coverage's own `fail_under`, NOT by
    # check_coverage.DEFAULT — that is the PER-MODULE floor for a file in
    # neither STRICT_MODULES nor RELAXED_PREFIXES (check_coverage.py's `else`
    # branch). The two are equal today by coincidence, and comparing the
    # rulebook's overall figure against DEFAULT left `fail_under` free to
    # drift: dropping it to 60 passed this gate. Compare both, separately.
    fail_under = _fail_under(pyproject)
    if fail_under is None:
        lines.append("FAIL pyproject.toml has no [tool.coverage.report] fail_under")
    elif overall != fail_under:
        lines.append(
            f"FAIL AGENTS.md overall {overall:g}% != "
            f"pyproject fail_under {fail_under:g}%"
        )
    if default != fail_under and fail_under is not None:
        lines.append(
            f"FAIL check_coverage DEFAULT {default:g}% != "
            f"pyproject fail_under {fail_under:g}% — the per-module default and "
            "the overall floor have drifted apart; AGENTS.md states one number"
        )
    if relaxed != relaxed_c:
        lines.append(
            f"FAIL AGENTS.md app.* {relaxed:g}% != "
            f"check_coverage RELAXED {relaxed_c:g}%"
        )
    if strict != strict_c:
        lines.append(
            f"FAIL AGENTS.md money-path {strict:g}% != "
            f"check_coverage STRICT {strict_c:g}%"
        )
    want = set(consts["STRICT_MODULES"])  # type: ignore[arg-type]
    got = {_module_to_path(m) for m in modules}
    if got != want:
        lines.append(
            "FAIL AGENTS.md coverage modules "
            f"{sorted(got)} != STRICT_MODULES {sorted(want)}"
        )
    prefixes = tuple(consts["RELAXED_PREFIXES"])  # type: ignore[arg-type]
    if "zarabot/app/" not in prefixes:
        lines.append(
            f"FAIL RELAXED_PREFIXES {prefixes} does not include zarabot/app/ "
            "(AGENTS.md names app.*)"
        )
    extra_pref = [p for p in prefixes if p != "zarabot/app/"]
    if extra_pref:
        lines.append(
            f"FAIL RELAXED_PREFIXES has extra {extra_pref} not named in AGENTS.md"
        )


def evaluate(
    root: pathlib.Path,
    *,
    extra_packages: dict[str, str] | None = None,
    missing_paths: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    extras = KNOWN_EXTRA_ZARABOT_PACKAGES if extra_packages is None else extra_packages
    missing = KNOWN_MISSING_SKETCH_PATHS if missing_paths is None else missing_paths
    lines: list[str] = []
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    spec = (root / "technical-spec.md").read_text(encoding="utf-8")
    coverage_src = (root / "scripts" / "ci" / "check_coverage.py").read_text(
        encoding="utf-8"
    )
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    _check_claude(root, lines)
    _check_rules(spec, agents, lines)
    _check_tree(root, agents, lines, extras, missing)
    _check_coverage(agents, coverage_src, pyproject, lines)
    failed = [line for line in lines if line.startswith("FAIL")]
    if failed:
        return 1, lines
    lines.append(
        "PASS rulebook: §8 range, named sketch directories, coverage constants"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
