# Proposal: #22 single-instance lock at startup

**Kind:** spec then `app.startup` (not `execution.orders`). In-process `asyncio.Lock` cannot coordinate two containers. A flock beside `DB_PATH` is a startup obligation: refuse to start if held.

**Should say (app.startup, before any broker call after DB path is known):** take an exclusive `flock` on `{db_path}.instance-lock` (or contract-named path), hold for process lifetime, `StartupError` + alert if already locked. Crash releases the lock. Restarts succeed.

**§3.2:**

- Second `start()` against the same `DB_PATH` while the first holds the lock raises `StartupError` and places no order.
- Killing the first process lets the second start.

**Modules:** `32-app-startup`. Do not fold into `execution.orders` (money module; locks there remain in-process serialisation).

**Severity:** `area:reliability` / serious. Human should read before amend; this changes deploy (two compose projects, accidental double `python -m zarabot`).
