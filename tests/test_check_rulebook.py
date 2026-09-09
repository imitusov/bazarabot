"""Issue #118 item 7: AGENTS.md vs §8 ordinals, sketch dirs, coverage constants.

Fixtures, not only the live repo. The live-tree test is the smoke that today's
rulebook still matches; the rest prove the gate fails when they drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_rulebook.py"

_COVERAGE_SRC = """\
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
"""

_AGENTS = """\
# Project Rules — Zarabot

**This file is the single rulebook for every agent.** `CLAUDE.md` is a symlink to
it, so Claude Code and Cursor Agent read the same text and it cannot drift.

- Handle every failure per the spec's numbered error rules (1–3, plus 9b).

**Coverage.** 80% overall; 70% for `app.*` orchestration; **95% for
`risk.gate`, `risk.sizing`, `lifecycle.exits` and `execution.orders`** — a
missed branch in those four is a financial defect, not a coverage statistic.

## File structure

```
zarabot/          models, clock, config, logging_setup, pnl
  db/             migrations, connection
sandbox/          data, backtest, train   (never imported by zarabot/)
scripts/verify/   pre-development verification suite
tests/            one file per module
migrations/       NNN_description.sql, forward-only
vendor/           the broker SDK wheel
```
"""

_SPEC = """\
## 8. Error handling rules

1. **One** → x
2. **Two** → y
9b. **Quote** → z
3. **Three** → w

## 9. Dependencies
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_rulebook", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_rulebook"] = mod
    spec.loader.exec_module(mod)
    return mod


_PYPROJECT = """[tool.coverage.report]
fail_under = 80
"""


def _tree(
    tmp_path: Path,
    *,
    agents: str = _AGENTS,
    spec: str = _SPEC,
    coverage: str = _COVERAGE_SRC,
    pyproject: str = _PYPROJECT,
    extra_zarabot_pkg: str | None = None,
    extra_py: bool = False,
    skip_dirs: frozenset[str] = frozenset(),
) -> Path:
    (tmp_path / "AGENTS.md").write_text(agents, encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    (tmp_path / "technical-spec.md").write_text(spec, encoding="utf-8")
    cov = tmp_path / "scripts" / "ci" / "check_coverage.py"
    cov.parent.mkdir(parents=True, exist_ok=True)
    cov.write_text(coverage, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    named = [
        "zarabot/db",
        "sandbox",
        "scripts/verify",
        "tests",
        "migrations",
        "vendor",
    ]
    for rel in named:
        if rel in skip_dirs:
            continue
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    if extra_zarabot_pkg:
        (tmp_path / "zarabot" / extra_zarabot_pkg).mkdir(parents=True, exist_ok=True)
    if extra_py:
        (tmp_path / "zarabot" / "clock.py").write_text(
            "# extra file not in sketch as a path\n"
        )
        (tmp_path / "sandbox" / "exchange.py").write_text("# known unlisted module\n")
    return tmp_path


def test_matching_rulebook_passes(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, extra_py=True))
    assert code == 0, lines
    assert any("PASS" in line for line in lines)


def test_agents_range_behind_spec_fails(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("(1–3, plus 9b)", "(1–29)")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any(
        "1–3" in line or "1-3" in line or "range" in line.lower() for line in lines
    )


def test_agents_range_ahead_of_spec_fails(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("(1–3, plus 9b)", "(1–38, plus 9b)")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1


def test_missing_9b_in_agents_fails_when_spec_has_9b(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("(1–3, plus 9b)", "(1–3)")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any("9b" in line for line in lines)


def test_plus_9b_without_spec_9b_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("9b. **Quote** → z\n", "")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 1
    assert any("9b" in line for line in lines)


def test_9b_is_not_the_high_ordinal(tmp_path: Path) -> None:
    """Highest §8 ordinal is the largest integer; 9b does not raise the range to 9."""
    check = _load()
    spec = """\
## 8. Error handling rules

1. **One** → x
9b. **Quote** → z
2. **Two** → y

## 9. Dependencies
"""
    agents = _AGENTS.replace("(1–3, plus 9b)", "(1–2, plus 9b)")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec, agents=agents))
    assert code == 0, lines


def test_indented_numbered_lists_outside_section_8_are_ignored(tmp_path: Path) -> None:
    check = _load()
    spec = """\
## 7. Observability

99. **Not a rule** → ignore

## 8. Error handling rules

1. **One** → x
2. **Two** → y
9b. **Quote** → z
3. **Three** → w

## 9. Dependencies

40. **pip** — not a §8 rule.
"""
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 0, lines


def test_claude_symlink_is_not_parsed_twice(tmp_path: Path) -> None:
    check = _load()
    root = _tree(tmp_path)
    # A second parse of CLAUDE.md would double-count; the gate must not fail
    # a matching rulebook just because the symlink exists.
    code, lines = check.evaluate(root)
    assert code == 0, lines
    assert not any("CLAUDE.md" in line and "FAIL" in line for line in lines)


def test_claude_regular_file_fails_as_identity_not_as_second_range(
    tmp_path: Path,
) -> None:
    check = _load()
    root = _tree(tmp_path)
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").write_text(
        _AGENTS.replace("(1–3, plus 9b)", "(1–1)"), encoding="utf-8"
    )
    code, lines = check.evaluate(root)
    assert code == 1
    assert any("symlink" in line.lower() for line in lines)


def test_named_directory_missing_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, skip_dirs=frozenset({"vendor"})))
    assert code == 1
    assert any("vendor" in line for line in lines)


def test_unexpected_zarabot_package_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, extra_zarabot_pkg="widgets"))
    assert code == 1
    assert any("widgets" in line for line in lines)


def test_unexpected_zarabot_package_allowlisted(tmp_path: Path) -> None:
    check = _load()
    root = _tree(tmp_path, extra_zarabot_pkg="widgets")
    code, lines = check.evaluate(root, extra_packages={"widgets": "#0"})
    assert code == 0, lines
    assert any("#0" in line for line in lines)


def test_unlisted_py_files_do_not_fail(tmp_path: Path) -> None:
    """The sketch lists packages, not every file. sandbox/exchange.py must not red."""
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, extra_py=True))
    assert code == 0, lines


def test_allowlisted_missing_sketch_path(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path, skip_dirs=frozenset({"vendor"})),
        missing_paths={"vendor": "#0"},
    )
    assert code == 0, lines
    assert any("#0" in line for line in lines)


def test_coverage_numbers_must_match_constants(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("80% overall", "81% overall")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any(
        "80" in line or "81" in line or "overall" in line.lower() for line in lines
    )


def test_coverage_strict_modules_must_match(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("`execution.orders`", "`execution.fills`")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any("execution" in line for line in lines)


def test_coverage_relaxed_prefix_must_match_app(tmp_path: Path) -> None:
    check = _load()
    coverage = _COVERAGE_SRC.replace('("zarabot/app/",)', '("zarabot/other/",)')
    code, lines = check.evaluate(_tree(tmp_path, coverage=coverage))
    assert code == 1


def test_live_repo_passes() -> None:
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, lines


def test_overall_tracks_pyproject_fail_under_not_the_per_module_default() -> None:
    """`N% overall` is coverage's `fail_under`, not check_coverage.DEFAULT.

    DEFAULT is the per-module floor for a file in neither STRICT_MODULES nor
    RELAXED_PREFIXES. They are equal today by coincidence, and comparing the
    rulebook's overall figure against DEFAULT left `fail_under` free to drift —
    dropping it to 60 passed this gate before this test existed.
    """
    check = _load()
    assert check._fail_under(_PYPROJECT) == 80.0
    assert check._fail_under("[tool.coverage.report]\nfail_under = 60\n") == 60.0
    assert check._fail_under("[tool.coverage.report]\n") is None


def test_pyproject_fail_under_drift_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path, pyproject="[tool.coverage.report]\nfail_under = 60\n")
    )
    assert code == 1
    assert any("fail_under" in line for line in lines)


def test_missing_fail_under_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(
        _tree(tmp_path, pyproject="[tool.coverage.report]\nshow_missing = true\n")
    )
    assert code == 1
    assert any("fail_under" in line for line in lines)


# --- anti-vacuity: a gate whose input moves must fail, not pass ---------------
# check_events.py (#165) and check_docs.py (pre-#163) both shipped a silent PASS
# when the section they parse was renamed. The code here already fails loudly on
# each; nothing pinned it, so a refactor to `if hit is None: return []` would
# restore that defect with a green suite.


def test_renamed_file_structure_heading_fails(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("## File structure", "## Repository layout")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any("File structure" in line for line in lines)


def test_missing_sketch_fence_fails(tmp_path: Path) -> None:
    check = _load()
    start = _AGENTS.index("## File structure")
    agents = _AGENTS[:start] + "## File structure\n\nno fence here\n"
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any("File structure" in line for line in lines)


def test_renamed_coverage_paragraph_fails(tmp_path: Path) -> None:
    check = _load()
    agents = _AGENTS.replace("**Coverage.**", "**Coverage floors.**")
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1
    assert any("Coverage" in line for line in lines)


def test_missing_rule_range_sentence_fails(tmp_path: Path) -> None:
    check = _load()
    agents = "\n".join(
        line for line in _AGENTS.splitlines() if "numbered error rules" not in line
    )
    code, lines = check.evaluate(_tree(tmp_path, agents=agents))
    assert code == 1


def test_renamed_section_8_heading_is_a_fail_not_a_traceback(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("## 8. Error handling rules", "## 8. Error rules")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec))
    assert code == 1
    assert any(line.startswith("FAIL") for line in lines), lines
