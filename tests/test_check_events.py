"""§7.1 event-emission gate (issue #118 item 6). Fixture trees, not the live repo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_events.py"

_SESSION = """\
import logging
_LOG = logging.getLogger(__name__)
_LOG.info("o", extra={"event": "session_open", "trade_date": 1})
_LOG.info("c", extra={"event": "session_closed", "trade_date": 1})
"""

_HALT = """\
import logging
_LOG = logging.getLogger(__name__)
_LOG.critical("h", extra={"event": "halt_triggered", "reason": 1})
"""

SPEC = """\
## 7. Observability

### 7.1 Log events

| Event | Owner | Level | Required data fields |
|---|---|---|---|
| `widget_ok` | `pkg.mod` | INFO | `alpha`, `beta` |
| `session_open` / `session_closed` | `pkg.session` | INFO | `trade_date` |
| `halt_triggered` | `pkg.halt` | CRITICAL | `reason`, `daily_loss_pct` (only on X) |

## 8. Error handling rules
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_events", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_events"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, mod_src: str) -> Path:
    spec = tmp_path / "technical-spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    files = {
        "zarabot/pkg/mod.py": mod_src,
        "zarabot/pkg/session.py": _SESSION,
        "zarabot/pkg/halt.py": _HALT,
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_owning_module_emits_event_and_fields(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_LOG.info('ok', extra={'event': 'widget_ok', 'alpha': 1, 'beta': 2})\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines
    assert any("PASS" in line for line in lines)


def test_missing_extra_event_fails(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        "import logging\n_LOG = logging.getLogger(__name__)\n_LOG.info('nope')\n",
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
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_LOG.info('ok', extra={'event': 'widget_ok', 'alpha': 1})\n",
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
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_LOG.info('ok', extra={'event': 'widget_ok', 'alpha': 1})\n",
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
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_LOG.info('ok', extra={'event': 'widget_ok', 'alpha': 1, 'beta': 2})\n",
    )
    allow = {("widget_ok", "beta"): "#999 — stale"}
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", allow)
    assert code == 1
    assert any("stale" in line.lower() for line in lines)


def test_nested_if_spread_dict_is_one_complete_site(tmp_path: Path) -> None:
    """Compliant emit inside `if` with a locally-built **dict is not a phantom."""
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f(x):\n"
        "    if x:\n"
        "        d = {'a': 1, 'b': 2}\n"
        "        _LOG.info('m', extra={'event': 'w', **d})\n"
    )
    assert check.collect_emits(src) == {"w": [{"a", "b"}]}
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f(x):\n"
        "    if x:\n"
        "        d = {'alpha': 1, 'beta': 2}\n"
        "        _LOG.info('m', extra={'event': 'widget_ok', **d})\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines
    assert any("PASS" in line for line in lines)


def test_nested_for_with_try_spread_dict_is_one_complete_site() -> None:
    check = _load()
    for_src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f(rows):\n"
        "    for _ in rows:\n"
        "        d = {'a': 1, 'b': 2}\n"
        "        _LOG.info('m', extra={'event': 'w', **d})\n"
    )
    with_src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f(cm):\n"
        "    with cm:\n"
        "        d = {'a': 1, 'b': 2}\n"
        "        _LOG.info('m', extra={'event': 'w', **d})\n"
    )
    try_src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f():\n"
        "    try:\n"
        "        d = {'a': 1, 'b': 2}\n"
        "        _LOG.info('m', extra={'event': 'w', **d})\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert check.collect_emits(for_src) == {"w": [{"a", "b"}]}
    assert check.collect_emits(with_src) == {"w": [{"a", "b"}]}
    assert check.collect_emits(try_src) == {"w": [{"a", "b"}]}


def test_nested_function_sees_enclosing_dict(tmp_path: Path) -> None:
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def outer():\n"
        "    d = {'a': 1, 'b': 2}\n"
        "    def inner():\n"
        "        _LOG.info('m', extra={'event': 'w', **d})\n"
        "    inner()\n"
    )
    assert check.collect_emits(src) == {"w": [{"a", "b"}]}
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def outer():\n"
        "    d = {'alpha': 1, 'beta': 2}\n"
        "    def inner():\n"
        "        _LOG.info('m', extra={'event': 'widget_ok', **d})\n"
        "    inner()\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_module_level_dict_spread_inside_function(tmp_path: Path) -> None:
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_D = {'a': 1, 'b': 2}\n"
        "def f():\n"
        "    _LOG.info('m', extra={'event': 'w', **_D})\n"
    )
    assert check.collect_emits(src) == {"w": [{"a", "b"}]}
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "_D = {'alpha': 1, 'beta': 2}\n"
        "def f():\n"
        "    _LOG.info('m', extra={'event': 'widget_ok', **_D})\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_emit_in_for_header_is_visible(tmp_path: Path) -> None:
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def _emit(level, event, **fields):\n"
        "    _LOG.log(level, event, extra={'event': event, **fields})\n"
        "def f():\n"
        "    for row in _emit(logging.INFO, 'widget_ok', alpha=1, beta=2):\n"
        "        pass\n"
    )
    assert check.collect_emits(src)["widget_ok"] == [{"alpha", "beta"}]
    root = _tree(tmp_path, src)
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_empty_event_table_fails(tmp_path: Path) -> None:
    check = _load()
    spec = tmp_path / "technical-spec.md"
    spec.write_text(
        "## 7. Observability\n\n### 7.2 Log events\n\n## 8. Error handling rules\n",
        encoding="utf-8",
    )
    (tmp_path / "zarabot").mkdir()
    code, lines = check.evaluate(spec, tmp_path / "zarabot", {})
    assert code == 1
    joined = "\n".join(lines)
    assert "spec §7.1 event table not found" in joined


def test_async_method_helper_counts_as_extra_event(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "class Owner:\n"
        "    async def _emit(self, level, event, **fields):\n"
        "        _LOG.log(level, event, extra={'event': event, **fields})\n"
        "    async def go(self):\n"
        "        await self._emit(logging.INFO, 'widget_ok', alpha=1, beta=2)\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_emit_helper_counts_as_extra_event(tmp_path: Path) -> None:
    check = _load()
    root = _tree(
        tmp_path,
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def _emit(level, event, **fields):\n"
        "    _LOG.log(level, event, extra={'event': event, **fields})\n"
        "_emit(logging.INFO, 'widget_ok', alpha=1, beta=2)\n",
    )
    code, lines = check.evaluate(root / "technical-spec.md", root / "zarabot", {})
    assert code == 0, lines


def test_inner_subscript_does_not_leak_to_outer_emit() -> None:
    """Inner `d["leak"]=1` must not satisfy an outer `**d` site."""
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "d = {'a': 1}\n"
        "def inner():\n"
        "    d['leak'] = 1\n"
        "_LOG.info('m', extra={'event': 'w', **d})\n"
    )
    assert check.collect_emits(src) == {"w": [{"a"}]}


def test_parameter_shadow_does_not_inherit_outer_dict_keys() -> None:
    """`def inner(d)` rebinds `d`; `**d` must not carry the outer keys."""
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "d = {'a': 1, 'b': 2}\n"
        "def inner(d):\n"
        "    _LOG.info('m', extra={'event': 'w', **d})\n"
    )
    assert check.collect_emits(src) == {"w": [set()]}


def test_inner_rebind_of_event_name_does_not_invent_outer_event() -> None:
    """Rebinding `E` inside `def` must not emit the outer string as a site."""
    check = _load()
    src = (
        "import logging\n"
        "_LOG = logging.getLogger(__name__)\n"
        "E = 'w1'\n"
        "def f():\n"
        "    E = 'w2'\n"
        "    extra = {'event': E, 'a': 1}\n"
        "    _LOG.info('m', extra=extra)\n"
    )
    sites = check.collect_emits(src)
    assert sites == {"w2": [{"a"}]}
    assert "w1" not in sites


def test_check_events_module_is_importable() -> None:
    if not _CHECK.is_file():
        pytest.fail("scripts/ci/check_events.py is missing")
    _load()
