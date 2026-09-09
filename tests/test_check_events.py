"""§7.1 event-emission gate (issue #118 item 6). Fixture trees, not the live repo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_events.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_events", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_events"] = mod
    spec.loader.exec_module(mod)
    return mod


SPEC = """\
## 7. Observability

### 7.1 Log events

| Event | Owner | Level | Required data fields |
|---|---|---|---|
| `widget_ok` | `pkg.mod` | INFO | `alpha`, `beta` |
| `session_open` / `session_closed` | `pkg.session` | INFO | `trade_date` |
| `halt_triggered` | `pkg.halt` | CRITICAL | `reason`, `daily_loss_pct` (only on a `DAILY_LOSS_LIMIT` halt) |

## 8. Error handling rules
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    spec = tmp_path / "technical-spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_owning_module_emits_event_and_fields(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('widget_ok', extra={'event': 'widget_ok', 'alpha': 1, 'beta': 2})\n"
            ),
            "zarabot/pkg/session.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "event = 'session_open'\n"
                "_LOG.info(event, extra={'event': event, 'trade_date': 'd'})\n"
                "event = 'session_closed'\n"
                "_LOG.info(event, extra={'event': event, 'trade_date': 'd'})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "extra = {'event': 'halt_triggered', 'reason': 'x'}\n"
                "_LOG.critical('halt_triggered', extra=extra)\n"
            ),
        },
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines
    assert any("PASS" in line for line in lines)


def test_missing_extra_event_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": "import logging\n_LOG = logging.getLogger(__name__)\n_LOG.info('nope')\n",
            "zarabot/pkg/session.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('session_open', extra={'event': 'session_open', 'trade_date': 1})\n"
                "_LOG.info('session_closed', extra={'event': 'session_closed', 'trade_date': 1})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.critical('h', extra={'event': 'halt_triggered', 'reason': 1})\n"
            ),
        },
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 1
    joined = "\n".join(lines)
    assert "widget_ok" in joined
    assert "missing event" in joined.lower() or "not emitted" in joined.lower()


def test_missing_field_key_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('widget_ok', extra={'event': 'widget_ok', 'alpha': 1})\n"
            ),
            "zarabot/pkg/session.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('session_open', extra={'event': 'session_open', 'trade_date': 1})\n"
                "_LOG.info('session_closed', extra={'event': 'session_closed', 'trade_date': 1})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.critical('h', extra={'event': 'halt_triggered', 'reason': 1})\n"
            ),
        },
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 1
    joined = "\n".join(lines)
    assert "widget_ok" in joined
    assert "beta" in joined


def test_allowlisted_gap_passes_with_inventory(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('widget_ok', extra={'event': 'widget_ok', 'alpha': 1})\n"
            ),
            "zarabot/pkg/session.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('session_open', extra={'event': 'session_open', 'trade_date': 1})\n"
                "_LOG.info('session_closed', extra={'event': 'session_closed', 'trade_date': 1})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.critical('h', extra={'event': 'halt_triggered', 'reason': 1})\n"
            ),
        },
    )
    allow = {("widget_ok", "beta"): "#999 — example gap until the producer lands"}
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", allow)
    assert code == 0, lines
    joined = "\n".join(lines)
    assert "widget_ok" in joined
    assert "beta" in joined
    assert "#999" in joined
    assert "allowlist" in joined.lower() or "allowlisted" in joined.lower()


def test_stale_allowlist_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('widget_ok', extra={'event': 'widget_ok', 'alpha': 1, 'beta': 2})\n"
            ),
            "zarabot/pkg/session.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('session_open', extra={'event': 'session_open', 'trade_date': 1})\n"
                "_LOG.info('session_closed', extra={'event': 'session_closed', 'trade_date': 1})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.critical('h', extra={'event': 'halt_triggered', 'reason': 1})\n"
            ),
        },
    )
    allow = {("widget_ok", "beta"): "#999 — stale"}
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", allow)
    assert code == 1
    assert any("stale" in line.lower() for line in lines)


def test_emit_helper_counts_as_extra_event(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        {
            "zarabot/pkg/mod.py": (
                "import logging\n"
                "_LOG = logging.getLogger(__name__)\n"
                "def _emit(level, event, **fields):\n"
                "    _LOG.log(level, event, extra={'event': event, **fields})\n"
                "_emit(logging.INFO, 'widget_ok', alpha=1, beta=2)\n"
            ),
            "zarabot/pkg/session.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.info('session_open', extra={'event': 'session_open', 'trade_date': 1})\n"
                "_LOG.info('session_closed', extra={'event': 'session_closed', 'trade_date': 1})\n"
            ),
            "zarabot/pkg/halt.py": (
                "import logging\n_LOG = logging.getLogger(__name__)\n"
                "_LOG.critical('h', extra={'event': 'halt_triggered', 'reason': 1})\n"
            ),
        },
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_check_events_module_is_importable() -> None:
    if not _CHECK.is_file():
        pytest.fail("scripts/ci/check_events.py is missing")
    _load()
