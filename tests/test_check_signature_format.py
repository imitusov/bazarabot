"""Issue #118 gate 1, format half (#116): §4 signatures must be readable.

`check_docs.py` check 3 diffs §4 signatures against `interfaces.md`, and it
parses with `SIG`, which requires ``→`` and a plain function name. A §4
signature that misses either is not *reported* as different — it is not seen at
all, so the drift gate is silently narrower than the section it claims to
cover (failure class 6).

Fixtures, not only the live repo. The live-tree test is the smoke that today's
§4 is readable; the rest prove the gate fails on each unreadable shape, and —
requirement 2 — that it fails LOUDLY when its own anchors move.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_signature_format.py"

_SPEC = """\
## 3. Tests

**`not_a_contract(x) → int`**

## 4. Module contracts

### `zarabot/models.py`

**`build(ticker: str, lots: int = 1) → Position`**
- A contract line.

**`async fetch(key: str) → list[Candle] | None`**
- Another.

**`PriceRejected`**
- An exception class, not a signature.

## 5. Tables
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_signature_format", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_signature_format"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, *, spec: str = _SPEC) -> Path:
    (tmp_path / "technical-spec.md").write_text(spec, encoding="utf-8")
    return tmp_path


def _fails(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


# --- the readable baseline -------------------------------------------------


def test_readable_signatures_pass(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), known={})
    assert code == 0, lines
    assert any(line.startswith("PASS") for line in lines)


def test_non_signature_bold_code_is_ignored(tmp_path: Path) -> None:
    """``**`PriceRejected`**`` is a class name, not a callable."""
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), known={})
    assert code == 0
    assert not any("PriceRejected" in line for line in lines)


def test_signatures_outside_section_4_are_ignored(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path), known={})
    assert code == 0
    assert not any("not_a_contract" in line for line in lines)


# --- requirement 1: every unreadable shape ---------------------------------


def test_missing_arrow_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(
        "**`build(ticker: str, lots: int = 1) → Position`**",
        "**`build(ticker: str, lots: int = 1)`**",
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("build" in line and "→" in line for line in _fails(lines)), lines


def test_ascii_arrow_is_accepted(tmp_path: Path) -> None:
    """`check_docs.SIG` accepts `->`, so this gate must not be stricter."""
    check = _load()
    spec = _SPEC.replace(") → Position`**", ") -> Position`**")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 0, lines


def test_empty_return_type_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(") → Position`**", ") → `**")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("build" in line for line in _fails(lines)), lines


def test_untyped_parameter_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("build(ticker: str, lots: int = 1)", "build(ticker, lots: int)")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("ticker" in line for line in _fails(lines)), lines


def test_default_without_a_type_fails(tmp_path: Path) -> None:
    """`lots = 1` reads as typed to a human and is not."""
    check = _load()
    spec = _SPEC.replace("lots: int = 1", "lots = 1")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("lots" in line for line in _fails(lines)), lines


def test_bare_star_and_slash_are_not_parameters(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(
        "build(ticker: str, lots: int = 1)", "build(ticker: str, /, *, lots: int = 1)"
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 0, lines


def test_no_parameters_passes(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("build(ticker: str, lots: int = 1)", "build()")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 0, lines


def test_nested_brackets_in_a_type_are_one_parameter(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace(
        "build(ticker: str, lots: int = 1)",
        "build(rows: dict[str, list[int]], lots: int = 1)",
    )
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 0, lines


def test_dotted_name_the_drift_gate_cannot_parse_fails(tmp_path: Path) -> None:
    """The live `backtest.run` shape: invisible to check_docs check 3."""
    check = _load()
    spec = _SPEC.replace("**`build(", "**`backtest.build(")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("backtest.build" in line for line in _fails(lines)), lines


# --- requirement 2: fail loudly when the input moves -----------------------


def test_renamed_section_4_anchor_fails(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("## 4. Module contracts", "## 4bis. Module contracts")
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("## 4" in line for line in _fails(lines))


def test_section_4_with_no_signatures_fails(tmp_path: Path) -> None:
    """Zero parsed signatures is a broken parse, never a clean bill of health."""
    check = _load()
    spec = "## 4. Module contracts\n\nProse only.\n\n## 5. Tables\n"
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known={})
    assert code == 1
    assert any("no signature" in line.lower() for line in _fails(lines))


# --- requirement 4: allowlist staleness ------------------------------------


def test_allowlisted_signature_passes_with_a_pass_line(tmp_path: Path) -> None:
    check = _load()
    spec = _SPEC.replace("**`build(", "**`backtest.build(")
    known = {"backtest.build": "#999 — a recorded spec defect"}
    code, lines = check.evaluate(_tree(tmp_path, spec=spec), known=known)
    assert code == 0, lines
    assert any("allowlisted" in line and "backtest.build" in line for line in lines)


def test_stale_allowlist_entry_for_absent_signature_fails(tmp_path: Path) -> None:
    check = _load()
    known = {"gone.function": "#999"}
    code, lines = check.evaluate(_tree(tmp_path), known=known)
    assert code == 1
    assert any("stale" in line.lower() and "gone.function" in line for line in lines)


def test_stale_allowlist_entry_for_a_now_readable_signature_fails(
    tmp_path: Path,
) -> None:
    check = _load()
    known = {"build": "#999 — was untyped"}
    code, lines = check.evaluate(_tree(tmp_path), known=known)
    assert code == 1
    assert any("stale" in line.lower() and "build" in line for line in lines)


# --- the live tree ---------------------------------------------------------


def test_live_repository_passes() -> None:
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, "\n".join(lines)


def test_live_tree_parses_the_whole_of_section_4() -> None:
    """A parse that finds a handful must never read as a clean §4."""
    check = _load()
    spec = (ROOT / "technical-spec.md").read_text(encoding="utf-8")
    found = check.section_4_signatures(spec)
    assert len(found) > 100, len(found)
