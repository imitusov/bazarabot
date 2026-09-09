# Proposal: #107 S-17 — rules 11 and 12 name `aiosqlite.Error`

**Kind:** spec. §4 already narrowed DB writes to `aiosqlite.Error` (v1.59)
in seven places. §8 rules 11 and 12 still read as a generic write failure /
`except Exception`. This amendment describes that code. **Rules 13 and 18
are out of this proposal** and wait for a later PR: `telegram.notifier`
(`notifier.py:44`, `:68`) and `ops.backup` (`backup.py:57`) still catch
bare `Exception`. Do not write that those modules already retry or that
code already narrowed them.

**Should say:** rule 11/12: `aiosqlite.Error` only.

**Also amend (or ask):** enable ruff `BLE001`. It was never selected.
`pyproject.toml:24` is `["E","F","W","I","UP","B","ASYNC","DTZ","S","RET","SIM"]`
— no `BLE`, and nothing mechanically enforces narrow catches. Do not claim
“BLE001 stays.”

**Seam with #105 / rule 8:** a third bare `except Exception` at
`app/startup.py:297` swallows `get_instrument` failure. Rule 8 says
unavailable at startup → `StartupError`. Name **`app.startup`**. Not a
rules 11/12 re-run.

**Do not:** pull rules 13 or 18 into this amendment.

**Modules:** spec §8 rules 11 and 12. No module re-run. `pyproject.toml`
lint select if BLE is accepted. `app.startup` only via #105 for `:297`.
