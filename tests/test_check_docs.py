"""Tests for scripts/ci/check_docs.py — two-module §4 headings and version gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CHECK_DOCS = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "check_docs.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_docs", _CHECK_DOCS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_two_module_heading_keys_body_to_each_zarabot_path() -> None:
    check = _load()
    spec = (
        "### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`\n"
        "\n"
        "**`async write_daily(snapshot: DailySnapshot) → None`**\n"
    )
    mapped = check.sections(spec, r"^### (.+)")
    assert "zarabot.db.signals" in mapped
    assert "zarabot.db.snapshots" in mapped
    assert "write_daily" in check.signatures(mapped["zarabot.db.snapshots"])


def test_two_module_heading_fails_when_second_module_signature_disagrees() -> None:
    """Today the heading keys only the first module, so this drift is invisible."""
    check = _load()
    spec = (
        "### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`\n"
        "\n"
        "**`async record(signal: Signal, decision: RiskDecision) → None`**\n"
        "**`async write_daily(snapshot: DailySnapshot) → None`**\n"
    )
    iface = (
        "## `zarabot.db.signals`\n"
        "\n"
        "**`async record(signal: Signal, decision: RiskDecision) → None`**\n"
        "\n"
        "## `zarabot.db.snapshots`\n"
        "\n"
        "**`async write_daily(snapshot: WrongType) → None`**\n"
    )
    drift, _stale = check.find_signature_drift(spec, iface, known={})
    names = {(mod, fn) for mod, fn, _want, _got in drift}
    assert ("zarabot.db.snapshots", "write_daily") in names


def test_function_recorded_in_neither_section_is_skipped_not_failed() -> None:
    check = _load()
    spec = (
        "### `zarabot/db/signals.py`, `zarabot/db/snapshots.py`\n"
        "\n"
        "**`async only_in_spec() → None`**\n"
    )
    iface = (
        "## `zarabot.db.signals`\n"
        "\n"
        "**`async record(signal: Signal, decision: RiskDecision) → None`**\n"
        "\n"
        "## `zarabot.db.snapshots`\n"
        "\n"
        "**`async write_daily(snapshot: DailySnapshot) → None`**\n"
    )
    drift, _stale = check.find_signature_drift(spec, iface, known={})
    assert drift == []


def test_version_header_older_than_own_citation_fails() -> None:
    check = _load()
    body = "**Version:** 1.69\n\nAmended in (v1.70) to change a contract.\n"
    declared, highest = check.header_vs_own_citations(body)
    assert declared == (1, 69)
    assert highest == (1, 70)
    assert declared < highest


def test_version_header_equal_to_own_citation_passes() -> None:
    check = _load()
    body = "**Version:** 1.70\n\nAmended in (v1.70) to change a contract.\n"
    declared, highest = check.header_vs_own_citations(body)
    assert declared == (1, 70)
    assert highest == (1, 70)
    assert declared >= highest


def test_version_header_newer_than_own_citation_passes() -> None:
    check = _load()
    body = "**Version:** 1.71\n\nAmended in (v1.70) to change a contract.\n"
    declared, highest = check.header_vs_own_citations(body)
    assert declared == (1, 71)
    assert highest == (1, 70)
    assert declared >= highest


def test_brief_citation_does_not_force_spec_header_bump() -> None:
    check = _load()
    body = (
        "**Version:** 1.70\n"
        "\n"
        "**Implements:** `business-brief.md` v1.71\n"
        "See also (brief v1.71).\n"
    )
    declared, highest = check.header_vs_own_citations(body)
    assert declared == (1, 70)
    assert highest == (0, 0)
    assert declared >= highest


def test_version_gate_does_not_pool_across_documents() -> None:
    check = _load()
    spec = "**Version:** 1.70\n\n**Implements:** `business-brief.md` v1.71\n"
    brief = "**Version:** 1.11\n"
    dep = "**Version:** 1.4\n\n**Derived from:** `technical-spec.md` v1.23\n"
    failures = check.version_gate_failures(
        {
            "technical-spec.md": spec,
            "business-brief.md": brief,
            "dependency-order.md": dep,
        }
    )
    assert failures == []


def test_brief_parenthesised_own_citation_fails() -> None:
    check = _load()
    body = "**Version:** 1.11\n\nAmended (v1.12)\n"
    declared, highest = check.header_vs_own_citations(body, "business-brief.md")
    assert declared == (1, 11)
    assert highest == (1, 12)
    assert declared < highest


def test_brief_bare_brief_v_citation_is_own_and_fails() -> None:
    check = _load()
    body = "**Version:** 1.11\n\nAmended in brief v1.12\n"
    declared, highest = check.header_vs_own_citations(body, "business-brief.md")
    assert declared == (1, 11)
    assert highest == (1, 12)
    assert declared < highest


def test_brief_filename_citation_is_own_and_fails() -> None:
    check = _load()
    body = "**Version:** 1.11\n\nAmended in `business-brief.md` v1.12\n"
    declared, highest = check.header_vs_own_citations(body, "business-brief.md")
    assert declared == (1, 11)
    assert highest == (1, 12)
    assert declared < highest


def test_dep_order_filename_citation_is_own_and_fails() -> None:
    check = _load()
    body = "**Version:** 1.4\n\n`dependency-order.md` v1.5\n"
    declared, highest = check.header_vs_own_citations(body, "dependency-order.md")
    assert declared == (1, 4)
    assert highest == (1, 5)
    assert declared < highest


def test_version_gate_fails_on_brief_own_filename_citation() -> None:
    check = _load()
    failures = check.version_gate_failures(
        {
            "business-brief.md": (
                "**Version:** 1.11\n\nAmended in `business-brief.md` v1.12\n"
            ),
        }
    )
    assert any("business-brief.md" in line for line in failures)
