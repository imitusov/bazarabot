"""Issue #183 F-57: compose log rotation vs the brief.

Fixtures, not only the live repo. The live-tree test is the smoke that today's
compose files still match the brief; the rest plant each drift and prove the
gate goes red — including the two ways a gate rots quietly, a renamed key and a
reworded brief.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CHECK = ROOT / "scripts" / "ci" / "check_compose.py"

_LOCAL = """\
services:
  zarabot:
    build: .
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
"""

_DEPLOY = """\
services:
  zarabot:
    image: ${IMAGE_REF:?set IMAGE_REF}
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
"""

_BRIEF = """\
- Logs rotate — the deployed configuration keeps five files of ten megabytes —
  and the database does not.
"""


def _load():
    spec = importlib.util.spec_from_file_location("check_compose", _CHECK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_compose"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path,
    *,
    local: str | None = _LOCAL,
    deploy: str | None = _DEPLOY,
    brief: str | None = _BRIEF,
) -> Path:
    if local is not None:
        (tmp_path / "docker-compose.yml").write_text(local, encoding="utf-8")
    if deploy is not None:
        (tmp_path / "docker-compose.deploy.yml").write_text(deploy, encoding="utf-8")
    if brief is not None:
        (tmp_path / "business-brief.md").write_text(brief, encoding="utf-8")
    return tmp_path


def test_live_repo_passes() -> None:
    check = _load()
    code, lines = check.evaluate(ROOT)
    assert code == 0, lines


def test_matching_fixtures_pass(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path))
    assert code == 0, lines
    assert any(line.startswith("PASS") for line in lines)


def test_deploy_max_file_below_brief_fails(tmp_path: Path) -> None:
    """The exact #183 defect: three files deployed, five promised."""
    check = _load()
    deploy = _DEPLOY.replace('max-file: "5"', 'max-file: "3"')
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("max-file 3" in line for line in lines)
    assert any("deployed file is the one that runs" in line for line in lines)


def test_both_compose_files_drifting_together_still_fails(tmp_path: Path) -> None:
    """Agreeing with each other is not enough — the brief is the senior document."""
    check = _load()
    local = _LOCAL.replace('max-file: "5"', 'max-file: "9"')
    deploy = _DEPLOY.replace('max-file: "5"', 'max-file: "9"')
    code, lines = check.evaluate(_tree(tmp_path, local=local, deploy=deploy))
    assert code == 1
    assert sum("max-file 9" in line for line in lines) == 2


def test_max_size_mismatch_fails(tmp_path: Path) -> None:
    check = _load()
    deploy = _DEPLOY.replace('max-size: "10m"', 'max-size: "50m"')
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("max-size 50m" in line for line in lines)


def test_max_size_in_kilobytes_fails(tmp_path: Path) -> None:
    check = _load()
    deploy = _DEPLOY.replace('max-size: "10m"', 'max-size: "10k"')
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("unit" in line for line in lines)


def test_renamed_max_file_key_fails(tmp_path: Path) -> None:
    """A moved key must FAIL, not silently pass."""
    check = _load()
    deploy = _DEPLOY.replace('max-file: "5"', 'maxfile: "5"')
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("exactly one 'max-file:'" in line for line in lines)


def test_missing_logging_block_fails(tmp_path: Path) -> None:
    check = _load()
    deploy = "services:\n  zarabot:\n    image: x\n"
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("no 'logging:' block" in line for line in lines)


def test_non_json_file_driver_fails(tmp_path: Path) -> None:
    check = _load()
    deploy = _DEPLOY.replace("driver: json-file", "driver: local")
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("json-file" in line for line in lines)


def test_duplicate_max_file_fails(tmp_path: Path) -> None:
    """Two services with different rotation would make the comparison meaningless."""
    check = _load()
    deploy = _DEPLOY + '        max-file: "5"\n'
    code, lines = check.evaluate(_tree(tmp_path, deploy=deploy))
    assert code == 1
    assert any("found 2" in line for line in lines)


def test_reworded_brief_sentence_fails(tmp_path: Path) -> None:
    """The gate's own input moving must FAIL, not silently pass."""
    check = _load()
    brief = _BRIEF.replace("deployed configuration keeps", "deploy config retains")
    code, lines = check.evaluate(_tree(tmp_path, brief=brief))
    assert code == 1
    assert any("found 0" in line for line in lines)


def test_duplicate_brief_sentence_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, brief=_BRIEF + _BRIEF))
    assert code == 1
    assert any("found 2" in line for line in lines)


def test_unknown_number_word_in_brief_fails(tmp_path: Path) -> None:
    check = _load()
    brief = _BRIEF.replace("five files", "thirteen files")
    code, lines = check.evaluate(_tree(tmp_path, brief=brief))
    assert code == 1
    assert any("unrecognised number word" in line for line in lines)


def test_missing_deploy_file_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, deploy=None))
    assert code == 1
    assert any("docker-compose.deploy.yml is missing" in line for line in lines)


def test_missing_brief_fails(tmp_path: Path) -> None:
    check = _load()
    code, lines = check.evaluate(_tree(tmp_path, brief=None))
    assert code == 1
    assert any("business-brief.md is missing" in line for line in lines)


def test_main_returns_exit_code(tmp_path: Path, monkeypatch) -> None:
    check = _load()
    monkeypatch.chdir(_tree(tmp_path))
    assert check.main() == 0
