# Proposal: #100 S-10 — `in_closing_window` lives on `SessionInfo`

**Kind:** spec. `lifecycle.exits.evaluate` says `session.in_closing_window(now)`
with `session: SessionInfo`. §4 `market.session` defines a **module** function
`in_closing_window(now, minutes)`. §4 `models` says dataclasses have
validation only, never logic.

Built: `SessionInfo.in_closing_window(now, minutes=15)` in `zarabot/models.py`
and `interfaces.md`.

**Should say:** `SessionInfo.in_closing_window(now, minutes=15)` is an
exception to “never logic” (or: it is a derived predicate from `start`/`end`,
still pure). `lifecycle.exits` calls that method. The module-level function
in `market.session` is either the same predicate delegated, or is deleted from
§4 if unused.

**Do not:** move I/O onto `SessionInfo`. Do not change the 15-minute default
in this amendment unless the brief does.

**Modules:** spec `models` + `lifecycle.exits` + `market.session` signatures.
Code already matches interfaces.
