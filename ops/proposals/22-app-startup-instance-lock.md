# Proposal: #22 single-instance lock at startup

**Kind:** spec then `app.startup` (not `execution.orders`). In-process
`asyncio.Lock` cannot coordinate two processes. A `flock` beside `DB_PATH` is a
startup obligation: refuse to start if held.

**Modules to re-run if amended:** `32-app-startup`, and nothing else. See
"Why one module" below — the scope really is one, and the reason is worth
writing down (failure class 1 is scope *under*-counted, but an over-counted
scope drags a money module into a reliability fix).

**Severity:** `area:reliability` / serious. Read before amending: this changes
what a hand-run `python -m zarabot` does on the VPS.

---

## 1. What actually breaks, on a named path

Two instances, both past `market.session`, both holding a signal for `SBER`.

`execution.orders.open_position` (`zarabot/execution/orders.py:418`) is entered
under `_locks(signal.ticker)` (`:132`). Those are `asyncio.Lock` objects rebuilt
per event loop by `_loop_locks` (`:98`); they serialise coroutines inside one
loop and are invisible to a second process. So both instances run:

1. `_already_open(ticker)` (`:151`) → `db.positions.list_open()` → **both see no
   OPEN row.**
2. `record_submitting(key, …)` (`:454`) with `key = str(uuid4())` — two
   *different* keys, two rows, both inserts succeed.
3. `post_market_order(key, figi, BUY, lots)` (`:465`) → `broker.client` passes
   `order_id=key` (`zarabot/broker/client.py:731`). **Two distinct market buys
   reach the exchange.** Money is spent twice.
4. `open_row(...)` (`:576`). The first commits. The second violates
   `idx_positions_one_open` (`migrations/001_initial.sql:84`), which raises
   `aiosqlite.IntegrityError`; `_write` (`:158`) catches `aiosqlite.Error`,
   halts, alerts and re-raises.

The halt is the only thing that stops it, and it fires *after* the money is
gone. What is left behind is worse than the halt:

- The broker holds **2× lots** of `SBER`. The local row says 1× lots.
- The exchange stop placed by `_place_stop` (`:232`) covers **1× lots**. Half
  the holding has no stop. The brief's stop-loss guarantee does not hold for it.
- `broker.reconcile` does **not** report this as `FOREIGN_HOLDING`. That branch
  (`reconcile.py:474`) only fires for a ticker with *no* local row. A ticker
  with a local row whose lot count differs takes `_adjust_lots`
  (`reconcile.py:258`), which emits **`LOTS_ADJUSTED`** and silently calls
  `update_lots(local.id, broker_lots)` — the position quietly becomes 2× lots at
  the *original* `entry_price`, so realised P&L is computed against a cost basis
  for half the shares. `app.startup._apply_remedies` classes `LOTS_ADJUSTED` as
  observed-not-remedied (`startup.py:65`), so no stop is placed for the extra
  lots. Rule 32's refusal never fires.
- `reconcile` is called from exactly one place — `app/startup.py:509`. It does
  **not** run in the trading loop. So none of the above is even detected until
  the next restart.

`reconcile.py:249` already says the quiet part: the tie-break there exists for
"the state the per-ticker submission lock is supposed to make impossible."

Two other paths, same cause:

- `close_position` (`:599`): both instances `get_position` → OPEN, both cancel
  the standing stop (the second is a tolerated `NOT_FOUND`,
  `client.py:820`), both submit a full-size SELL. The second sale is a short.
  `confirm_margin_trade=False` should make the broker refuse it; nothing in this
  repo makes that certain, and "the broker will probably refuse" is not the
  no-leverage guarantee the brief makes.
- `_place_stop` from both instances → two live stop orders on one position. That
  is the `STOP_DUPLICATE` double-sell condition (#35). It is detected — at the
  next startup.

## 2. What the idempotency key already covers, and what it does not

`key` is a fresh `uuid4()` minted per submission inside `open_position` /
`close_position` and passed to the broker as `order_id`. `CLAUDE.md`'s "recovery
is by querying with the idempotency key, never by sending again" is about
`resolve_unfinished` (`:753`) → `get_order_state(order.key)`: a crash between
`record_submitting` and the broker's reply leaves a `SUBMITTING` row, and the
next start asks the broker what happened to *that key* instead of buying again.

That is a **crash-recovery** guarantee, and it is real. It is not a
**concurrency** guarantee, and it cannot become one: two instances mint two
different UUIDs, so the broker sees two unrelated orders and correctly fills
both. The key deduplicates a retry of the same intent; it cannot deduplicate two
independently-formed intents. **The fix is not smaller than the issue implies.**

The database's real cross-process guarantees are also worth stating, because
they are easy to over-read. `db.connection.transaction()` uses `BEGIN IMMEDIATE`
under WAL with `busy_timeout = 30000` (`db/connection.py:61-63,107`), so each
*individual* transaction is atomic across processes. Every check-then-act that
spans two transactions with a broker call between them is not, and that is the
whole of `open_position`. (The issue text says `db.orders.settle` has "no
transaction, `orders.py:137-163`" — **that is stale**: `settle` wraps its
SELECT-then-UPDATE in `transaction()` (`db/orders.py:148`). The `#20` half of
this finding is already done for `settle`.)

## 3. How a second instance actually starts

- **A hand-run `python -m zarabot` on the VPS.** `/opt/zarabot/data` is a bind
  mount (`docker-compose.deploy.yml`), so the live database is an ordinary host
  path. This is the realistic vector, and it is likeliest during an incident,
  which is the worst moment.
- **`docker run -v /opt/zarabot/data:/data …`** to try an image by hand. Bypasses
  compose entirely.
- **A second clone whose compose file bind-mounts the same data directory.**
- Not a vector: a second `docker compose up` in `$REPO_DIR`.
  `docker-compose.deploy.yml` sets `container_name: zarabot`, and the project
  name is the directory basename, so compose recreates rather than duplicates,
  and a foreign project fails on the name clash. That is an accident, not a
  control, and it disappears the day someone drops `container_name`.

Every vector shares one property: **the same `DB_PATH` on the same host.** That
is exactly what an OS-level lock on a file beside the database covers, and it is
why a deploy-script guard is the wrong answer — `update.sh` is not in the path
of any of the three real vectors.

## 4. Proposed contract text

### 4a. §4 `zarabot/app/startup.py` — new step 2b, between step 2 and step 3

> 2b. **Take the single-instance lock, and hold it for the life of the process.**
> Open `Path(str(config.db_path) + ".instance-lock")` for writing, creating it if
> absent, and take an exclusive non-blocking `flock` on it. If it is already
> held, alert and raise `StartupError` naming the path. No database connection is
> opened, no broker call is made, and no order is placed on that path.
>
> The descriptor is kept for the lifetime of the process and is **never closed by
> application code** — not by `app.shutdown`, not by an exception handler, not by
> a context manager. The kernel releases the lock when the process ends, by any
> means, including `SIGKILL`, an OOM kill, `docker kill` and power loss, so a
> crashed instance never leaves a lock a human has to clear. That is the entire
> reason the lock is a `flock` and not a PID file, a `runtime_state` row or a
> heartbeat: those record liveness, and a liveness record written by a process
> that then dies is a latch with no reset (failure class 8) — on a live account,
> with open positions, at 3am. Restarts are routine (failure class 15; six in one
> evening during #45), so the release must cost nobody anything.
>
> It is `flock` (`LOCK_EX | LOCK_NB`), not `fcntl.lockf`. `flock` locks belong to
> the *open file description*, so two acquisitions within one process contend
> exactly as two processes do — which is what makes this guard testable at all
> (failure class 4: a guard that cannot fire is worse than none). POSIX record
> locks belong to the process, would be granted twice inside one process, and are
> dropped by *any* `close()` of *any* descriptor on the file.
>
> The lock is on a file beside `DB_PATH`, not on the database.
> `PRAGMA locking_mode = EXCLUSIVE` is incompatible with WAL and would lock out
> `scripts/deploy/update.sh`'s read-only in-flight-order query and
> `scripts/deploy/export_health.py`. **The database offers no mutual exclusion
> that would serve here:** WAL plus `BEGIN IMMEDIATE` and `busy_timeout = 30000`
> make each individual transaction atomic across processes and nothing more.
>
> It runs at step 2b — after `logging_setup.configure`, so the refusal is
> redacted and structured, and before step 3, so a refused instance never opens
> the database. **This is a held lock, not a periodic check.** A check at startup
> would say nothing about an instance that starts a minute later; a lock held for
> the process lifetime refuses a second instance starting at any later moment,
> for one `flock` call per process and no per-cycle cost.
>
> **On refusal, emit `startup_failed` (CRITICAL) with `stage` `instance` and
> `reason` `INSTANCE_LOCKED`.** No new §7.1 row: `startup_failed` already exists
> and this module already owns it.

### 4b. §4 `app.startup` — the `stage` enumeration

Replace:

> `stage` (the step name: `config`, `logging`, `database`, `strategies`,
> `session`, `recovery`, `reconcile`, `halt`, `ready`)

with:

> `stage` (the step name: `config`, `logging`, `instance`, `database`,
> `strategies`, `session`, `recovery`, `reconcile`, `halt`, `reachability`,
> `ready`)

`reachability` is a **pre-existing drift**, unrelated to #22:
`app/startup.py:518` already sets it and the enumeration never listed it. It is
in the same sentence, so fixing it here costs nothing.

### 4c. §3.2 `app.startup` — three cases

> - A second `start()` against the same `DB_PATH` while the first instance's lock
>   is held raises `StartupError`, opens no database connection, and places no
>   order (proves the guard refuses rather than trading a second time — the
>   `asyncio.Lock`s in `execution.orders` coordinate coroutines in one event loop
>   and cannot see another process at all).
> - Releasing the lock the way the kernel releases it — closing the descriptor —
>   lets the next `start()` succeed (proves a crashed instance does not leave a
>   bot that will not start; restarts are routine, and refusing to start is
>   strictly worse than the duplicate it prevents when there is nothing to
>   duplicate).
> - The refusal emits `startup_failed` with `stage` `instance`, emits no
>   `startup_ok`, and the alert names the lock path (proves the deploy health gate
>   and the owner both see a refusal that is not a broker fault).

Both of the first two are deterministic in one process and one event loop: no
subprocess, no `sleep`, no timing. The `flock`-vs-`lockf` choice in 4a is what
buys that, verified on this kernel — a second `open()` + `flock(LOCK_EX|LOCK_NB)`
in the same process returns `EWOULDBLOCK`, and closing the first descriptor makes
the next acquisition succeed.

### 4d. §10 deployment, and `.gitignore`

- §10: name `${DB_PATH}.instance-lock` as a runtime artefact created beside the
  database in the bind-mounted data directory. It must be writable by uid 1000
  (the directory already is), it is not backed up, and `ops.backup` does not copy
  it (`_prune_sync` globs `zarabot-*.db` only).
- `.gitignore`: add `*.instance-lock`. `data/`, `*.db`, `*.db-wal` and `*.db-shm`
  are already ignored, but a developer whose `DB_PATH` is `./zarabot.db` gets
  `./zarabot.db.instance-lock`, which none of those patterns match.

## 5. Open decisions the amender must settle — do not let an implementer guess

**D1 — the restart loop, and the alert that never stops.** A refused instance
alerts, `__main__` sleeps 30s and returns 1, and `restart: unless-stopped`
starts it again. If the second instance is under compose, that is one Telegram
message every 30 seconds, forever — failure class 14, in a channel whose premise
is that silence means healthy, and #1 is already open on the restart loop. Three
options:

  a. Leave it. The refusal is loud, self-inflicted, and the operator who caused
     it is the one being paged.
  b. Exit **0** on this one `StartupError` so the restart policy does not
     restart it. This contradicts `__main__`'s stated contract ("non-zero on
     `StartupError`") and hides a real fault from `docker ps`.
  c. Latch the alert to the first refusal per lock file. This reintroduces a
     latch with no durable reset — in the module that just refused to open the
     only durable store there is.

Recommend **(a)**, with the reasoning written into the contract so the next
audit does not re-open it as failure class 14. The 30s sleep already bounds it,
and unlike a broker outage this condition has a human on the other end of it.

**D2 — who must NOT take the lock.** State explicitly that
`scripts/deploy/export_health.py`, `scripts/deploy/update.sh`, `ops.backup` and
everything under `sandbox/` are readers and never acquire it. Without that
sentence the next agent adds a `flock` to `export_health.py` and the nightly
health export starts failing precisely whenever the bot is up.

**D3 — nothing changes in `execution.orders`.** Its `asyncio.Lock`s stay exactly
as they are. They are intra-process serialisation between the trading loop, the
Telegram command handler and the shutdown drain; they remain necessary and
sufficient for that, and they are not a cross-process control and were never
claimed to be. This proposal adds no obligation to that module, which is why the
re-run list is one module long: `app.shutdown` is untouched *because* nothing
releases the lock.

**D4 — the secondary finding in the issue** (`_ticker_locks` grows without
bound) is not worth a contract line. It is bounded by watchlist size, and
`_loop_locks` (`:98`) already discards the dict whenever the event loop changes.
Recommend recording it as declined in the issue rather than amending for it.

## 6. What this deliberately does not cover

- Two instances against **different** hosts sharing a network filesystem.
  `flock` over NFS is unreliable. The deployment is one VPS with local disk; if
  that ever changes, this control silently weakens and the amendment should say
  so.
- An instance already running when the lock is first deployed. The rollout is a
  normal container replacement — old container stops, then the new one starts —
  so there is no overlap under `update.sh`; a hand-started process from before
  the upgrade holds no lock and will not be seen.
- The transactional gaps of #20 beyond `db.orders.settle`. The lock makes them
  unreachable in practice; it does not close them.
