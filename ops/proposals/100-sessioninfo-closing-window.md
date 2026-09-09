# Proposal: #100 S-10 — `in_closing_window` lives on `SessionInfo`

**Kind:** spec. `lifecycle.exits.evaluate` says `session.in_closing_window(now)`
with `session: SessionInfo`. §4 `market.session` defines a **module** function
`in_closing_window(now, minutes)`. §4 `models` says dataclasses have
validation only, never logic.

Built: `SessionInfo.in_closing_window(now, minutes=15)` in `zarabot/models.py`
and `interfaces.md`. The module-level function has **no production caller**
(grep across `zarabot/` finds it only in `tests/test_market_session.py`).

**Should say:** `SessionInfo.in_closing_window(now, minutes=15)` is an
exception to “never logic” (or: it is a derived predicate from `start`/`end`,
still pure). `lifecycle.exits` calls that method. **Delete** the module-level
`market.session.in_closing_window`. Strike `interfaces.md` (the module-level
signature) and the §4 claim that it is “used only by the maximum-age exit”
— that caller is false (`evaluate` uses the `SessionInfo` method, which is
exactly the kind of false caller `check_docs.py --docs` exists to catch).

**Do not:** move I/O onto `SessionInfo`. Do not change the 15-minute default
in this amendment unless the brief does. Do not keep an unused delegating
module function.

**Modules:** spec `models` + `lifecycle.exits` + `market.session` (delete
the module function); `interfaces.md`. Prefer deletion.
