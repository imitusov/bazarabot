#!/usr/bin/env python3
"""Fail when the two compose files' log rotation drifts from the brief.

Issue #183 (F-57). ``docker-compose.deploy.yml`` kept three log files while
``docker-compose.yml`` kept five and ``business-brief.md`` promised five.
Nothing noticed, and the number is load-bearing: ``scripts/deploy/
export_health.py`` reads ``docker compose logs --since {days*24}h``, so a
shorter retention makes a busy week look like a quiet one — a shortfall that
is indistinguishable from "nothing happened".

This gate therefore:

  * extracts ``logging.options.max-size`` and ``max-file`` from BOTH
    ``docker-compose.yml`` and ``docker-compose.deploy.yml``, requiring a
    ``logging:`` block with ``driver: json-file`` and exactly one occurrence
    of each key in each file — a renamed or deleted key is a FAIL, not a
    silent pass;
  * parses the brief's sentence ``the deployed configuration keeps
    <N> files of <M> megabytes`` (English number words), requiring exactly
    one such sentence — a reworded brief is a FAIL, not a silent pass;
  * requires the two compose files to agree with each other and with the
    brief on both numbers, with ``max-size`` expressed in megabytes.

WHAT THIS DOES NOT COVER:

  * Every other key in the two compose files. Only the ``logging`` options
    are compared; ``image``/``build``, volumes and env deliberately differ
    between local and deploy, and enumerating the intended differences here
    would encode today's diff as a rule.
  * The logging driver's own defaults. If a compose file omits ``logging``
    entirely this gate fails, but it does not model what Docker would do.
  * Any third compose file. The two paths are named constants
    (``LOCAL``/``DEPLOY``); a new ``docker-compose.*.yml`` is invisible to
    this gate until added to them.
  * ``compress``, ``max-buffer-size`` or other json-file options.
  * Whether the running container actually uses the committed file. This is
    a repository check, not a check against the VPS.
  * ``export_health.py``'s ``--days`` default vs the retained window. The
    retained bytes depend on log volume, which is not knowable statically.
"""

from __future__ import annotations

import pathlib
import re
import sys

LOCAL = "docker-compose.yml"
DEPLOY = "docker-compose.deploy.yml"
BRIEF = "business-brief.md"

_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_LOGGING = re.compile(r"(?m)^\s*logging:\s*$")
_DRIVER = re.compile(r"(?m)^\s*driver:\s*json-file\s*$")
_MAX_SIZE = re.compile(r"(?m)^\s*max-size:\s*\"?(\d+)([kKmMgG])\"?\s*$")
_MAX_FILE = re.compile(r"(?m)^\s*max-file:\s*\"?(\d+)\"?\s*$")
_BRIEF_SENTENCE = re.compile(
    r"deployed configuration keeps ([a-z]+) files of ([a-z]+) megabytes",
)


class ComposeError(ValueError):
    """A compose file's logging block is missing, renamed or duplicated."""


def compose_rotation(name: str, text: str) -> tuple[int, str, int]:
    """Return ``(max_size_value, max_size_unit, max_file)`` for one compose file.

    Raises ``ComposeError`` when the block or either key is absent, renamed,
    or present more than once. Silence on a moved key is the whole failure
    this gate exists to prevent, so every such case is loud.
    """
    if _LOGGING.search(text) is None:
        raise ComposeError(f"{name} has no 'logging:' block")
    if _DRIVER.search(text) is None:
        raise ComposeError(f"{name} logging block has no 'driver: json-file'")
    sizes = _MAX_SIZE.findall(text)
    files = _MAX_FILE.findall(text)
    if len(sizes) != 1:
        raise ComposeError(
            f"{name} must contain exactly one 'max-size:' option, found {len(sizes)}"
        )
    if len(files) != 1:
        raise ComposeError(
            f"{name} must contain exactly one 'max-file:' option, found {len(files)}"
        )
    return int(sizes[0][0]), sizes[0][1].lower(), int(files[0])


def brief_rotation(text: str) -> tuple[int, int]:
    """Return ``(files, megabytes)`` promised by the brief.

    Raises ``ComposeError`` when the sentence is missing, duplicated, or
    written with a number word this gate does not know.
    """
    hits = _BRIEF_SENTENCE.findall(text)
    if len(hits) != 1:
        raise ComposeError(
            f"{BRIEF} must contain exactly one 'deployed configuration keeps "
            f"<N> files of <M> megabytes' sentence, found {len(hits)}"
        )
    files_word, size_word = hits[0]
    try:
        return _NUMBER_WORDS[files_word], _NUMBER_WORDS[size_word]
    except KeyError as exc:
        raise ComposeError(
            f"{BRIEF} log-rotation sentence uses unrecognised "
            f"number word {exc.args[0]!r}"
        ) from None


def evaluate(root: pathlib.Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    parsed: dict[str, tuple[int, str, int]] = {}
    for name in (LOCAL, DEPLOY):
        path = root / name
        if not path.is_file():
            lines.append(f"FAIL {name} is missing")
            continue
        try:
            parsed[name] = compose_rotation(name, path.read_text(encoding="utf-8"))
        except ComposeError as exc:
            lines.append(f"FAIL {exc}")

    brief_path = root / BRIEF
    promised: tuple[int, int] | None = None
    if not brief_path.is_file():
        lines.append(f"FAIL {BRIEF} is missing")
    else:
        try:
            promised = brief_rotation(brief_path.read_text(encoding="utf-8"))
        except ComposeError as exc:
            lines.append(f"FAIL {exc}")

    if len(parsed) == 2:
        local, deploy = parsed[LOCAL], parsed[DEPLOY]
        if local != deploy:
            lines.append(
                f"FAIL {LOCAL} keeps {local[2]} files of {local[0]}{local[1]} but "
                f"{DEPLOY} keeps {deploy[2]} files of {deploy[0]}{deploy[1]} — the "
                "deployed file is the one that runs"
            )

    if promised is not None:
        want_files, want_mb = promised
        for name, (size, unit, count) in sorted(parsed.items()):
            if unit != "m":
                lines.append(
                    f"FAIL {name} max-size unit is {unit!r}; {BRIEF} states megabytes"
                )
            elif size != want_mb:
                lines.append(
                    f"FAIL {name} max-size {size}m != {BRIEF}'s {want_mb} megabytes"
                )
            if count != want_files:
                lines.append(
                    f"FAIL {name} max-file {count} != {BRIEF}'s {want_files} files"
                )

    failed = [line for line in lines if line.startswith("FAIL")]
    if failed:
        return 1, lines
    size, unit, count = parsed[DEPLOY]
    lines.append(
        f"PASS log rotation: {LOCAL} and {DEPLOY} both keep {count} files of "
        f"{size}{unit}, matching {BRIEF}"
    )
    return 0, lines


def main() -> int:
    code, lines = evaluate(pathlib.Path("."))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
