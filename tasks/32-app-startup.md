# Task 32/43: Implement `zarabot/app/startup.py`

## Product context

Fixed startup ordering: config, logging, connection, migrations, strategies, session, order recovery, reconciliation, halt state, ready alert. 70% coverage.

## Build order position

Module **32** of 43 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/startup.py`

**`async start() → AppContext`**

Fixed ordering; each step completes before the next begins:
1. `config.load()` — abort on failure before anything else, including any network call.
   **When `load()` raises `ConfigError`, this module is still the owner of
   `config_invalid` (v1.61).** Logging is not configured yet. `start()` therefore
   calls `logging_setup.configure` with whatever token and account-id values are
   already in the environment (empty list if none), emits `config_invalid`
   (CRITICAL) with `variable` from the `ConfigError`, then raises `StartupError`.
   It never proceeds to a broker call. `config` itself does not emit the event:
   it has no logger of its own by design.
1b. Write `SSL_TBANK_VERIFY` into the process environment from
   `config.ssl_tbank_verify`. This must precede every broker call; a channel
   created before it is set fails its TLS handshake.
1c. **When `ssl_tbank_verify` is false, alert the owner before the first broker
   call**, saying that certificate verification is disabled on the connection
   carrying the trading token. `config` logs it; a log line on a server nobody
   is watching is not a security control. The alert must never contain the
   token.
2. `logging_setup.configure()`, passing every token and account identifier on
   the loaded `Config` as `secrets`.

2b. **Take the single-instance lock, and hold it for the life of the process
   (v1.89, #22).** Open `Path(str(config.db_path) + ".instance-lock")` for
   writing, creating it if absent, and take an exclusive non-blocking `flock`
   on it. If the lock is already held, refuse to start: raise `StartupError`
   naming the lock path, having emitted `startup_failed` (CRITICAL) with
   `stage` `instance` and `reason` `INSTANCE_LOCKED`, and having alerted or
   not alerted per the once-per-condition rule below. **On the refusal path no
   database connection is opened, no broker call is made, and no order is
   placed.** No new §7.1 row is needed: `startup_failed` already exists and
   this module already owns it.

   **What the lock prevents, on the named path.** The submission locks in
   `execution.orders` are `asyncio.Lock` objects rebuilt per event loop; they
   serialise coroutines inside one loop and are invisible to a second process.
   Two instances against one `DB_PATH` therefore both pass `_already_open`,
   both `record_submitting` — with two *different* `uuid4()` keys, so both
   inserts succeed — and both `post_market_order`. **Two real market buys reach
   the exchange.** Only then does the second `open_row` violate
   `idx_positions_one_open` and halt, after the money has moved. The
   idempotency key cannot close this: it deduplicates a *retry of one intent*
   against `resolve_unfinished`, and two instances form two independent
   intents that the broker correctly fills as two unrelated orders.

   The aftermath is worse than the halt and is the reason this is a startup
   obligation rather than a nuisance. `broker.reconcile` does **not** class the
   doubled holding as `FOREIGN_HOLDING` — that candidate list is built from
   tickers *not present* in the local map, so a ticker that has a local row can
   never reach it. It takes `_adjust_lots` instead, which alerts and grows the
   row to 2× lots at the *original* entry price; `LOTS_ADJUSTED` is in this
   module's observed-not-remedied set, so **no stop is placed for the extra
   lots**. That missing remedy is **#233**, which has its own owner decision
   and is deliberately not settled here; what is settled here is that the
   condition stops being reachable.

   **The descriptor is kept for the lifetime of the process and is never closed
   by application code** — not by `app.shutdown`, not by an exception handler,
   not by a context manager. The kernel releases the lock when the process
   ends by any means, including `SIGKILL`, an OOM kill, `docker kill` and power
   loss, so a crashed instance never leaves a lock a human has to clear. That is
   the whole reason it is a `flock` and not a PID file, a `runtime_state` row or
   a heartbeat: those record liveness, and a liveness record written by a
   process that then dies is a latch with no reset — on a live account, holding
   open positions, at 3am. Restarts are routine, not exceptional, so the release
   must cost nobody anything.

   **It is `flock` (`LOCK_EX | LOCK_NB`), deliberately, and not
   `fcntl.lockf`.** `flock` locks belong to the *open file description*, so two
   acquisitions within one process contend exactly as two processes do — which
   is what makes this guard testable at all, in one process and one event loop,
   with no subprocess and no `sleep`. A guard that cannot be made to fire is
   worse than no guard, because it reads as protection. POSIX record locks
   belong to the *process*: they would be granted twice inside one process, and
   are dropped by *any* `close()` of *any* descriptor on the file.

   **The lock is on a file beside `DB_PATH`, never on the database itself.**
   `PRAGMA locking_mode = EXCLUSIVE` is incompatible with WAL and would lock out
   `scripts/deploy/export_health.py` and `scripts/deploy/update.sh`'s read-only
   in-flight-order query. The database offers no mutual exclusion that would
   serve here: WAL plus `BEGIN IMMEDIATE` and `busy_timeout` make each
   *individual* transaction atomic across processes and nothing more, and every
   check-then-act in `execution.orders` spans two transactions with a broker
   call between them.

   It runs at step 2b — after `logging_setup.configure`, so the refusal is
   redacted and structured and `startup_failed` is emittable, and before step 3,
   so a refused instance never opens the database. **This is a held lock, not a
   periodic check.** A check at startup would say nothing about an instance that
   starts a minute later; a lock held for the process lifetime refuses a second
   instance starting at any later moment, for one `flock` call per process and
   no per-cycle cost.

2c. **Alert once per refusal condition, then refuse quietly (v1.89, #22, owner
   decision).** A refused instance alerts the owner on the **first** refusal,
   and on every subsequent refusal logs and raises without alerting. The exit
   code is unchanged — `__main__` still sleeps 30 seconds and exits 1 on
   `StartupError`, and `startup_failed` is emitted on **every** refusal, first
   or repeat, so `docker ps`, the container log and the deploy health gate all
   still see a fault. Suppressing the *alert* is not suppressing the *failure*.
   Without this rule, `restart: unless-stopped` plus that 30-second sleep is one
   Telegram message every half minute forever, in a channel whose entire premise
   is that silence means healthy.

   **The restart is a new process, so the latch cannot live in the process.** A
   module-level boolean is reset by the very restart it is meant to survive. The
   marker is therefore a file beside the lock:
   `Path(str(config.db_path) + ".instance-lock.refused")`, containing a single
   ISO-8601 UTC timestamp taken from `clock.now()` — never `datetime.now()`,
   which this module may not call. On refusal:

   - **Marker absent, unreadable, malformed, or bearing an instant in the
     future** — treat as absent. This is a first refusal: write `clock.now()`
     into the marker (truncating), **alert**, emit `startup_failed`, raise.
     Unreadable and malformed fail *loud*: a corrupt marker must never be able
     to silence the channel, and a clock that moved backwards must not either.
   - **Marker present and its instant is within the ageing window** — a repeat
     refusal: **do not alert**, and **do not rewrite the marker**. Log at
     WARNING that the alert was suppressed, naming the marker's recorded
     instant, so an operator reading the log sees both the refusal and why it
     was quiet. Emit `startup_failed`, raise.
   - **Marker present and older than the ageing window** — treat as a first
     refusal: alert and rewrite it with the new instant.
   - **The marker cannot be written** (permissions, full disk) — alert anyway
     and continue to the `StartupError`. The marker is an anti-spam device and
     is never a precondition for the refusal.

   **Not rewriting the marker on a repeat refusal is load-bearing, not an
   omission.** The window is measured from the *first* refusal of a condition,
   not the most recent one. Refreshing it on every retry would push the instant
   forward every 30 seconds under exactly the restart loop this rule exists for,
   making the age-out unreachable and turning "alert once per condition" into
   "alert once, ever" — a latch whose reset is written down and cannot fire.

   **The marker's reset, stated because a latch that does not name one is the
   defect (failure class 8).** Two things clear it:

   1. **A successful acquisition of the lock deletes the marker**, as part of
      step 2b, before startup proceeds. This is the primary reset and it is
      reachable by construction: the moment an instance holds the lock is the
      moment there is exactly one instance, which is precisely when the refusal
      condition has ended. Every normal recovery — the operator kills the stray
      process, the container is replaced, the host reboots — passes through it.
   2. **Age-out after 24 hours.** A marker whose recorded instant is more than
      24 hours before `clock.now()` is treated as absent, so the next refusal
      alerts again and rewrites it.

   **Why 24 hours.** The floor is set by the retry period: the window must be far
   longer than the ~30-second restart cycle or the storm simply re-forms, so
   anything on the order of minutes is disqualified. The ceiling is set by how
   long a genuine new incident may stay quiet. 24 hours matches the cadence the
   owner already reads this channel on — the daily heartbeat is one message per
   Moscow day, so a permanently duplicated instance costs at most one extra
   message a day beside it, against 2,880 a day unlatched. It also means a
   persistent duplicate is re-reported daily rather than once ever, which is the
   difference between a latch and an amnesty.

   **The accepted risk, recorded as a decision rather than an oversight.**
   Reset (1) does not fire in the one case that matters most: the *holder* keeps
   running and never restarts, so nothing deletes the marker, and a genuine
   second incident weeks later is silent — until the age-out. The owner accepted
   this explicitly. The 24-hour age-out is the entire bound on it: without the
   age-out the exposure would be unbounded, and with it the worst case is that a
   new duplicate-instance incident is refused quietly for up to 24 hours before
   the owner is told. In that window the guard is still doing its job — no
   second instance trades — so what is delayed is the notification, never the
   protection.

   **`scripts/ci/check_latches.py` does not cover this latch, and must not be
   read as covering it (failure class 6).** That gate inspects module-level
   booleans and requires an assignment back to `False` in the same module.
   This latch is a file on disk, so the gate is structurally blind to it: a
   green run says nothing whatever about the marker, and would stay green if
   both resets above were deleted. The reset is enforced by this contract and by
   the §3.2 cases that exercise it, and by nothing else.

2d. **Who must never take this lock (v1.89, #22).**
   `scripts/deploy/export_health.py`, `scripts/deploy/update.sh`, `ops.backup`
   and everything under `sandbox/` are readers of the data directory and
   **never acquire the instance lock**, neither shared nor exclusive. Stated
   positively because the omission is the trap: a `flock` added to the health
   export makes the nightly export fail precisely whenever the bot is up, which
   is whenever it is healthy. The lock answers exactly one question — "is
   another *bot* running against this `DB_PATH`" — and every one of those four
   is something other than a bot.

2e. **Nothing changes in `execution.orders` (v1.89, #22).** Its `asyncio.Lock`s
   stay exactly as they are. They are intra-process serialisation between the
   trading loop, the Telegram command handler and the shutdown drain; they
   remain necessary and sufficient for that, they were never a cross-process
   control, and this step does not make them one. In particular, the issue's
   secondary finding — that `_ticker_locks` grows without bound — is
   **declined**, and the reason is verified rather than assumed: the dict is
   keyed by ticker and so bounded by watchlist size, and `_loop_locks` already
   discards the whole dict whenever the running event loop changes. It has no
   contract line because there is no behaviour to specify.

   **What this step deliberately does not cover.** Two instances on *different*
   hosts sharing a network filesystem: `flock` over NFS is unreliable, and the
   deployment is one VPS with local disk. If that ever changes, this control
   silently weakens and this paragraph is the notice. Nor does it see an
   instance that was already running when the lock was first deployed — a
   hand-started process from before the upgrade holds no lock. Nor does it close
   the transactional gaps of #20; it makes them unreachable in practice, which
   is not the same thing.

3. `db.connection.connect(config.db_path)`, then
   `db.migrations.apply(db.connection.shared())`. The connection is opened here —
   not at import, and not inside a repository — and `apply` receives the shared
   connection rather than opening a second one.
4. `strategies.registry.enabled()`, including model load if configured.
5. `market.session.refresh(days)`, with the caller's schedule window — 14 days.
6. `execution.orders.resolve_unfinished()`.
7. `broker.reconcile.reconcile()`, then apply its remedies via
   `execution.orders`: re-protect unprotected positions, cancel orphaned stops,
   replace mispriced ones, **replace missized ones**, and **resolve duplicates — for a `STOP_DUPLICATE`
   adjustment, cancel every identifier in its `cancel` list and retain `keep`.**
   Reconciliation identifies; the executor acts. Duplicates are cancelled
   through `execution.orders.cancel_orphaned_stop`, which is the only cancel
   primitive that module exposes; a duplicate settles as `ORPHANED` as a result,
   which is imprecise — it was a duplicate, not an orphan. A dedicated primitive
   belongs with the `execution.orders` batch rather than as a change made in
   passing to the module that moves money. The behaviour is safe meanwhile:
   `cancel_orphaned_stop` demotes a position to `LOCAL` only when the cancelled
   stop is the one recorded in `stop_order_key`, and that is the stop
   reconciliation chose to keep, so the retained stop is never disturbed.
   **An identifier in `cancel` that matches no known stop alerts and the sequence
   continues** — skipping it silently would reproduce #35 exactly, and refusing
   to start would leave a duplicate standing rather than remove the ones that can
   be removed. **Every adjustment type the report can carry is handled here.** An adjustment with no branch is silently
   dropped, which is what happened to `STOP_DUPLICATE`: the double-sell condition
   was detected, reported, and then ignored, and the ready alert counted it as
   one more adjustment (#35). An unrecognised adjustment type must alert rather
   than pass, so a report the executor does not understand is loud.

7a. **`EXIT_UNRESOLVED` is observed, not remedied, and does not stop startup
   (v1.37).** It is a position the broker no longer holds whose sale could not
   be found in the operations feed, so there is nothing for `execution.orders`
   to do about it — the shares are already gone. It belongs with
   `CLOSED_EXTERNALLY` and `ADOPTED` in the set of types this step recognises
   without acting on, precisely so it does not
   trip the "adjustment types this build cannot act on" alert, which is reserved
   for a report the executor genuinely does not understand.

   **`LOTS_ADJUSTED` left that set in v1.90 (#233), and `FOREIGN_HOLDING` was
   never in it in the sense the set's own description claims.** The set was
   described as "types reconciliation resolves itself, they need no remedy",
   which is true of `CLOSED_EXTERNALLY` and `ADOPTED` — the reconciliation *is*
   the fix — and was never true of `LOTS_ADJUSTED`: the row was corrected and
   the protection was not, which is the whole of #233. `FOREIGN_HOLDING` is
   acted on, by 7b's refusal. This step therefore recognises **three** groups,
   and the distinction is what stops the next reader re-deriving the wrong
   conclusion from the membership:

   - **Remedied here, through `execution.orders`** — `STOP_MISSING`,
     `STOP_MISPRICED`, `STOP_MISSIZED`, `STOP_ADOPTABLE`, `STOP_ORPHAN`,
     `STOP_DUPLICATE`.
   - **Acted on here, not through `execution.orders`** — `FOREIGN_HOLDING`,
     which 7b turns into a refusal to start.
   - **Recognised and correctly acted on by nobody** — `CLOSED_EXTERNALLY`,
     `ADOPTED`, `EXIT_UNRESOLVED`, and `LOTS_ADJUSTED`. The first two are
     complete when reconciliation writes them. `EXIT_UNRESOLVED` describes
     shares that are already gone. `LOTS_ADJUSTED` is here for a different
     reason and the reason must be written down: its row correction is
     complete, and the protection it disturbs is **not** its own remedy to
     carry — the same report carries `STOP_MISSIZED` for that position when the
     standing stop no longer covers it, derived from live state rather than
     from this adjustment. `LOTS_ADJUSTED` needs no branch because another type
     owns the consequence, not because there is no consequence.

   All three groups are "known" for the purposes of the unrecognised-type alert.
   An adjustment type in none of them must alert.

   Its consequence is named here rather than left to be discovered: the position
   stays open, so if it was `EXCHANGE` the same report will carry `STOP_MISSING`
   for it and this step will try to place a stop against shares the account does
   not hold. The broker refuses, the position stays `LOCAL`, and the owner is
   alerted — the degrade path rule 23 already defines. That is noisy and correct;
   the alternative was a fabricated exit price written permanently into the trade
   history.

7aa. **Every stop remedy in this step runs whatever `config.stop_loss_enabled`
   says (v1.93).** The flag decides whether a *new* position gets a stop; it
   decides nothing about the positions that already have one. `STOP_MISSING`
   arises only for an `EXCHANGE`-protected position, and such a position is one
   the owner's §9 decision deliberately left protected, so its stop is replaced
   here exactly as before — skipping the replacement because "stops are off"
   would leave a position whose row reads `EXCHANGE` with nothing watching it,
   which is #95 reproduced on purpose. `STOP_ORPHAN`, `STOP_DUPLICATE`,
   `STOP_MISPRICED` and `STOP_MISSIZED` likewise run unchanged. The one branch
   the flag reaches is `STOP_ADOPTABLE`, and it is reached by the finding never
   being reported at all — `broker.reconcile` withholds it under its own
   contract, so this step gains no condition and no branch is removed from it.
   An unrecognised-type alert is still owed if one arrives.

7b. **Refuse to start on a `FOREIGN_HOLDING` adjustment**, unless
   `config.allow_foreign_holdings` is true. Raise `StartupError` naming every
   ticker reported, after alerting. The account is the bot's alone (brief v1.8),
   and a holding the bot does not recognise means either that someone traded in
   it by hand or that local state is wrong — and the bot cannot tell which. When
   the flag is set, the holdings are named in the ready alert instead and are
   never traded: no stop is placed, no exit is evaluated, no sale is made.
   Refusing is the correct failure direction. The alternative failure is selling
   something the owner chose to hold, at a price they did not choose.

7c. **`STOP_MISSIZED` → `execution.orders.replace_stop(position, instrument)`
   (v1.90, #233, owner decision).** The branch is the one `STOP_MISPRICED`
   already takes and takes the same call: read the position from the refreshed
   open map, read the instrument, call `replace_stop`. `replace_stop` cancels the
   standing stop, demotes the position to `LOCAL` and clears `stop_order_key`,
   re-reads the position, and posts one stop for the position's current lot
   count. That is the owner's decision — replace the stop for the new size —
   expressed as a call that already exists rather than as a new primitive, and
   it satisfies both ownership rules by construction: exactly one stop is live
   at any moment, and the bot's own protection is armed for the interval in
   which the exchange's is not.

   **Cancel-then-place is the only shape available, and the order is binding.**
   Posting the correctly-sized stop first would put two live stops on one
   position, which is `STOP_DUPLICATE` — the double-sell condition — created
   deliberately. Leaving the undersized stop standing and demoting the position
   to `LOCAL` would put both owners on one position, which the rulebook forbids
   outright and for the same reason. There is no third arrangement.

   **The cancel succeeds and the placement fails → rule 23, unchanged. No new
   rule, no halt, no retry beyond the three rule 23 already specifies (v1.90,
   #233, settled).** `replace_stop` reaches `_place_stop`, which retries three
   times, alerts, emits `stop_protection_degraded` (ERROR), and leaves the
   position open and `LOCAL`. `LOCAL` is not "no stop": it is the ownership under
   which `lifecycle.exits` returns `STOP_LOSS` and
   `execution.orders.close_position` sells, and it sells `position.lots` — the
   count reconciliation has just made agree with the broker.

   **That outcome is better than the state the remedy found, not worse, and the
   comparison is the reason this needs no rule of its own.** Before: the row and
   the broker agree at the larger count, the exchange stop covers part of it,
   `stop_protection` reads `EXCHANGE`, and `lifecycle.exits` fires nothing —
   part of the holding is covered and *nothing is watching the rest*, with no
   alert standing. After a failed replacement: no exchange stop, protection
   `LOCAL`, the bot watching the whole holding at the corrected size, an alert
   sent and `stop_protection_degraded` on the record. The ordering is what makes
   this true and it is binding: the lot count is corrected by reconciliation in
   step 7's own call, before any remedy runs, so both the placement and the
   fallback act on the true size.

   The degrade is not free and is not claimed to be. The bot's poller runs on the
   trading loop's cadence and during market hours, where a resting exchange stop
   does not; what is traded away is a partial, unwatched exchange stop for full
   coverage that is slower and stops when the process does. Rule 23 already makes
   that trade for every position whose stop cannot be placed, and rule 23's own
   sentence settles the direction: "Never unwind a sound position because a
   secondary order failed." Halting is the wrong remedy here for a further
   reason — halting stops entries, and this position needs the trading loop
   *running* for `lifecycle.exits` to protect it, which is the argument step 8a
   already makes about refusing to start while holding open positions.

   **Rule 38's trade comes out the other way here, and that is deliberate.**
   Rule 38 withholds `STOP_MISPRICED` when the increment cannot be read, because
   spending an unprotected gap on a difference the bot cannot measure is a worse
   trade than leaving a possibly-stale stop standing. A missized stop is measured
   exactly — two integers from one round trip — so the difference is certain, and
   the coverage that a standing undersized stop provides is certainly partial.
   Rule 38 is about an unmeasurable discrepancy and does not reach a measured
   one.

   **An adjustment whose position is absent from the refreshed open map is
   skipped**, as `STOP_MISPRICED` and `STOP_MISSING` already are: the position
   closed between reconciliation and the remedy, and there is nothing to
   protect.
8. Restore halt state.

8a. **Report a position budget that cannot buy one lot.** For each ticker in
   `config.watchlist`, read the instrument and its last price, and compare
   `instrument.lot × price` against
   `risk.sizing.position_budget(config.allocated_capital, config.position_size_pct)`.

   - When **no** watchlist instrument is affordable, alert the owner that the bot
     cannot open a position in anything it is watching, naming the budget and the
     cheapest lot cost found.
   - When **some** are affordable, name the unaffordable ones in the ready alert
     of step 9 rather than raising a separate alert. A partially reachable
     watchlist is a normal operating condition — an instrument's price rises
     through the budget without anything being wrong — and must not train the
     owner to ignore the channel.
   - A ticker whose instrument or price cannot be read is **excluded from the
     judgement and named separately**, never counted as affordable and never
     counted as unaffordable. If no price could be read for any ticker, the check
     is **inconclusive** and says so; it must not report a blackout it did not
     observe, and it must not stay silent as though it had confirmed health.

   **This step owns rule 37 (v1.75).** The no-instrument-affordable alert is
   reported **once per start** — not per cycle and not per signal. At one poll a
   minute the per-signal alternative is several hundred identical messages a day,
   and an alert that repeats forever is equivalent to no alert in a channel whose
   premise is that silence means healthy. The `ZERO_LOTS` rejections themselves
   stay correct and stay silent; this rule governs only whether the owner is
   told. Process start *is* the reset: a restart is a reasonable moment to say it
   again, and nothing here is latched in the database.

   **Rule 8's startup half is an open decision and is not implemented here
   (v1.75).** This step reads each watchlist instrument and therefore looks like
   the place that would refuse to start on unreadable metadata. It does not, and
   the reasoning is under rule 8 in §8, where the decision is recorded for the
   owner. What is binding today is the paragraph below: no agent adds a
   `StartupError` to this step on its own reading of rule 8's word "must".

   **This step never raises `StartupError`, and never prevents startup.** An
   unaffordable budget stops *new entries only*. Refusing to start would
   additionally abandon every open position — no exit evaluation, no stop
   management, no `MAX_AGE` — converting a benign no-op into an unmanaged holding
   with real money in it. The bot must keep running to protect what it holds.

   It runs after step 7 so reconciliation has settled position truth first, and
   before step 9 so the finding can be folded into the ready alert. A broker
   failure in this step is alerted and startup continues; this is a diagnostic,
   and it must never be the reason the bot is not running.

8b. **Install the `/report` builder (v1.88, #36).** Call
   `telegram.commands.set_report_builder(reporter.weekly.build)`. Until that
   call is made `/report` replies `report unavailable` for the life of the
   process, so this step is the only thing that makes a command in the brief's
   command table work at runtime.

   The injection exists so that `telegram.commands` needs no import of
   `reporter.weekly`: the command module stays testable without the reporter,
   and the dependency points one way. That leaves the two ends joined by
   nobody, and `app.startup` is the composition point that joins them — which
   is why the obligation is recorded here rather than under
   `telegram.commands`, which owns the handler and not the wiring, or under
   `reporter.weekly`, which owns the builder and not the wiring.

   It runs after step 8a and before step 9 because step 9's alert says the bot
   is running, and the Telegram command listener `app.loops.run` starts once
   `start()` returns must find a builder already installed. Nothing later in
   `start()` may leave the builder cleared.

   **This is the `build_application` defect one module along, and that is why
   it is written down.** The call lived in the code from the first build and in
   no contract, so an agent regenerating this module from its task file had no
   reason to keep it; deleting it left `/report` permanently broken with every
   gate green. The contract is what makes the wiring an obligation rather than
   an accident.

8c. **Name the disabled stop in the ready alert when `config.stop_loss_enabled`
   is false (v1.93).** The alert of step 9 says, in words, that no protective
   stop is placed and that open positions exit on target or age only. It also
   names the count of open positions still carrying an exchange stop from before
   the flag was set, because those behave differently from every position opened
   since and the difference is invisible otherwise.

   **Cadence and reset.** Once per process start, in the existing ready alert —
   no separate message, no latch, and nothing to reset: the condition is a
   configuration value, it is re-read on every start, and it stops being
   reported the moment the flag is set back to true. It is deliberately not a
   repeating alert. A configured state the owner chose is not a fault, and an
   alert that fires on a cadence for a setting nobody is going to change is
   failure class 14. The on-demand half is `telegram.commands`' `/limits` and
   `/positions`, under its own heading.

9. Alert the owner that the bot is running, reporting version, mode, halt state
   and any reconciliation adjustments, **and emit the `startup_ok` log event of
   §7.1 carrying the same four facts** — `version`, `mode`, `halted`,
   `adjustments_count` (v1.58). The event is stated here, in the contract of the
   module that owes it, because §7.1 is a table of formats and a module never
   reads it as a work item: `startup_ok` was specified there from the first
   version and emitted by nothing, while `scripts/deploy/update.sh` greps the
   container's logs for it as its health gate and would have rolled back every
   deploy it ever ran. The Telegram alert and this event are deliberately
   redundant: one is for a person who may be asleep, the other for a deployer
   that cannot read Telegram.

- Raises `StartupError` on any failure, having alerted if Telegram credentials
  were valid. No entry may be attempted before step 9 completes.
- **This module owns rule 29 (v1.75).** Clock accuracy is a host requirement,
  not a runtime rule: V9 confirms the host is NTP-synchronised before deployment,
  and this module logs the observed system time in **both UTC and MSK** in the
  first log line after every restart, so a skewed clock is visible to whoever
  reads that line. **`_log_observed_time` writes it (v1.79)**, immediately after
  `logging_setup.configure` — the earliest point at which a line is redacted and
  structured, and therefore the first line this module writes after every
  restart. It carries `utc` and `msk` fields as well as the message: UTC alone
  cannot show a wrong Moscow offset and MSK alone cannot show a wrong instant,
  so both are needed to tell a drifted zone database from a drifted clock. It
  carries no `event` key and is not a §7.1 row. The observed instant comes from
  `clock.now()`, never from `datetime.now()`, which this module may not call.
  Rule 29 went unimplemented from the day it was written until #196, because no
  §4 contract claimed it (#115) — the rule existed and reached nobody. There is deliberately no runtime skew check, and adding one is
  not an improvement — the reasoning is in rule 29 and rests on the broker
  exposing no server wall-clock. The one timestamp available, `LastPrice.time`,
  is the time of the last *trade* and lags arbitrarily in a quiet market, so a
  check built on it would halt trading because nobody traded.
- **On any `StartupError` after logging is configured, emit `startup_failed`
  (CRITICAL) with `stage` (the step name: `config`, `logging`, `instance`,
  `database`, `strategies`, `session`, `recovery`, `reconcile`, `halt`,
  `reachability`, `ready`) and `reason` (v1.61; `instance` added v1.89 for step
  2b).** No `startup_ok` on that path. `__main__` does not emit either event; it
  only sleeps and exits.

  **`reachability` is a pre-existing drift, unrelated to #22 (v1.89).** The code
  has set that stage since step 8a was written and the enumeration never listed
  it, so a reader auditing the emitted values against this sentence would have
  found a value the spec did not admit. It is corrected in the same sentence
  step 2b had to edit anyway.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

7. **Broker and database disagree on positions or quantities** → the broker wins,
   the local record is corrected, and the owner is alerted with specifics.

   **Correcting a quantity is not the whole correction (v1.90, #233).** A
   standing stop was sized to the count the record held. Once the count moves,
   that stop covers the wrong quantity, and while `stop_protection` reads
   `EXCHANGE` the bot declines to fire `STOP_LOSS` — so a position can agree with
   the broker, read as reconciled, and be protected on part of itself with
   nothing watching the rest. The correction is complete only when the stop
   covers the corrected count. `broker.reconcile` reports the coverage
   discrepancy as `STOP_MISSIZED`, re-derived from live state on **every** pass
   rather than as a consequence of the quantity write, and `app.startup` step 7c
   remedies it through `execution.orders.replace_stop`. If that replacement
   cannot be placed, rule 23 applies unchanged and nothing here overrides it: the
   position degrades to `LOCAL` at the corrected count, which is more coverage
   than the undersized stop it replaced, not less.

   **The recorded entry price is not re-derived when a quantity grows, and the
   discrepancy is recorded instead.** The added lots are carried at the original
   `entry_price`, so realised P&L for that position is wrong by their true cost.
   Rule 33 forbids inventing the number, and the operations feed cannot supply it
   either: the feed cannot say which buys belong to this position, which is
   exactly what is unknown, and rule 32 exists because the bot cannot tell a
   manual purchase from wrong local state. The `position_events`
   `LOTS_ADJUSTED` row and the `reconciliations` row are the durable record, and
   the alert this rule already requires says so in the growth direction. A shrink
   needs no such note: the per-lot cost basis of the remaining lots is unchanged.

15. **Configuration missing or invalid at startup** → refuse to start, alert if
    Telegram credentials are among the valid ones, sleep 30 seconds, exit
    non-zero. The sleep exists so the container restart policy cannot produce an
    alert loop.

16. **Schema version ahead of the code** → refuse to start, alert, change nothing.

17. **Model file missing, unreadable, or with a mismatched feature manifest**
    while ML is enabled → refuse to start. A silently disabled model would mean
    trading a different system than the owner believes.

21. **Unhandled exception in a background task** → log with traceback, alert,
    restart that task with exponential backoff. One failing task must never
    terminate the process or any other task.

29. **Clock accuracy is a host requirement, verified at deployment, not a
    runtime rule.** V9 confirms the host clock is NTP-synchronised before the bot
    is deployed, and `app.startup` logs the observed system time in UTC and MSK
    so a skewed clock is visible in the first log line after every restart.

    There is deliberately **no runtime skew check**. The broker exposes no server
    wall-clock: the only timestamp available is `LastPrice.time`, which is the
    time of the last *trade* and lags arbitrarily when a market is quiet. Halting
    trading because nobody traded for ninety seconds would be a worse failure
    than the drift it guards against, and the alternative — shipping a
    hand-written NTP client into a system that moves money — is more risk than a
    correctly configured time daemon warrants.

30. **Database accessed before `db.connection.connect`, or after
    `disconnect`** → `DatabaseNotOpenError`. It must never open a fallback
    connection. This is a programming defect in the same family as rule 22: it
    fails loudly rather than reconnecting to a file nobody chose. A silent
    reconnect would hide a missing `app.startup` step in production, and in tests
    would let one test inherit a database another created.

32. **The broker reports a holding the bot has no record of at startup** →
    refuse to start, alert, and name every ticker, unless
    `config.allow_foreign_holdings` is true. The account is the bot's alone
    (brief v1.8). The bot cannot distinguish "someone bought this by hand" from
    "local state is wrong", and both readings forbid trading it. When the flag is
    set, the holdings are named in the ready alert and are never traded: no stop
    placed, no exit evaluated, no sale made. Never adopt one — adoption derived a
    stop and target from the holding's average cost, which handed the next cycle
    a position already past its take-profit.

37. **No instrument on the watchlist costs less than one position budget** →
    alert at startup, and continue running. The bot is watching a list it cannot
    afford to buy any of, and every signal it generates will be rejected
    `ZERO_LOTS` forever. The rejection itself is correct and stays correct — this
    rule governs only whether the owner is told. It is reported once per start
    rather than per cycle or per signal: at one poll a minute the per-signal
    alternative is several hundred identical messages a day, and an alert that
    repeats forever is equivalent to no alert in a channel whose premise is that
    silence means healthy.

    Where **some** instruments are affordable and some are not, the unaffordable
    ones are named in the ready alert and nothing is escalated. A watchlist the
    budget only partly reaches is a normal operating state, not a fault: on
    2026-08-28 MGNT and LKOH were out of reach while SBER and GAZP were buyable,
    and that configuration was working as intended.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Startup with valid config, a reachable broker and a clean database completes
  and reports ready (happy path).
- Invalid config aborts before any broker call is made, emits `config_invalid`
  with `variable`, and does not emit `startup_ok` (v1.61).
- `SSL_TBANK_VERIFY` is present in the environment before the first broker call
  (proves the TLS root is available when the channel is built — the failure this
  guards against is a handshake error that looks like a network fault rather
  than a configuration one).
- Starting with `ssl_tbank_verify` false alerts before any broker call, and the
  alert contains no token (proves running without certificate verification is
  something the owner is told about rather than something buried in a log).
- An unresolved order from a previous run is resolved before the first strategy
  evaluation (proves recovery precedes trading — the ordering that prevents a
  duplicate order).
- Reconciliation runs before the first entry is permitted (proves the same for
  position truth).
- A halted-at-shutdown bot starts halted (proves halt persistence end to end).
- A `STOP_DUPLICATE` adjustment causes every identifier in `cancel` to be
  cancelled and `keep` to be retained (proves the remedy is applied — it was
  reported and dropped, and every test still passed).
- A report containing **only** a `STOP_DUPLICATE` still applies it (proves the
  remedy gate does not skip a report that carries no other stop adjustment).
- **A `STOP_MISSIZED` adjustment for an open position of 4 lots whose live stop
  covers 2 leaves exactly one live stop, covering 4, and the stop that covered 2
  cancelled (v1.90, #233).** The assertions are the live stop set and its lot
  count, never that `replace_stop` was called: an implementation that cancelled
  and placed nothing, or placed a second stop beside the first, fails on the
  resulting set rather than on a call record (proves the remedy restores coverage
  for the true quantity, and that it does not create the double-sell condition
  while doing it).
- A report containing **only** a `STOP_MISSIZED` still applies it (proves the
  remedy gate does not skip a report that carries no other stop adjustment — the
  gate that dropped `STOP_DUPLICATE` in #35).
- A report carrying `LOTS_ADJUSTED` **and** `STOP_MISSIZED` for the same position
  replaces that position's stop exactly **once**, leaving one live stop (proves
  the two types are not both wired to a remedy: `LOTS_ADJUSTED` owns the row
  correction and `STOP_MISSIZED` owns the protection, and a second replacement
  would cancel the stop the first just placed).
- A report carrying **only** `LOTS_ADJUSTED` raises no "adjustment types this
  build cannot act on" alert and does not stop startup (proves it is still a
  recognised type after leaving the self-resolving group — the group it was in is
  what #233 was, and removing it from every group would turn the loud path into a
  false alarm on every lot correction).
- **A `STOP_MISSIZED` whose replacement placement is rejected three times leaves
  the position open, `stop_protection = LOCAL`, `stop_order_key` null and its lot
  count at the corrected 4; alerts; emits `stop_protection_degraded`; and
  `start()` completes and returns an `AppContext` (v1.90, #233).** The case then
  calls the real `lifecycle.exits.evaluate` on the returned position with a price
  below its stop and asserts it yields `STOP_LOSS` — a pure call, no stub — so
  the claim "`LOCAL` is real protection at the corrected size" is asserted rather
  than described (proves the failed replacement takes rule 23's degrade and not a
  halt, and that what the bot is now watching is the whole holding rather than
  the part the cancelled stop covered).
- An adjustment type the executor does not recognise alerts rather than being
  ignored (proves a report it cannot act on is loud).
- An `EXIT_UNRESOLVED` adjustment does **not** raise that alert and does not stop
  startup (proves the observed-not-remedied set includes it, so the loud path
  stays reserved for a report the executor genuinely does not understand).
- A `FOREIGN_HOLDING` adjustment raises `StartupError` naming the ticker, and no
  entry is attempted (proves the account-exclusivity policy is enforced rather
  than documented).
- With `allow_foreign_holdings` true, startup completes, the ready alert names
  the holding, and no stop is placed and no exit submitted for it (proves the
  acknowledged path is observe-only).
- **With the instance lock already held, `start()` raises `StartupError`, opens
  no database connection and places no order (v1.89, #22).** The case takes the
  real lock itself — `open` on `${DB_PATH}.instance-lock` plus
  `fcntl.flock(fd, LOCK_EX | LOCK_NB)` — and then calls the real `start()`. It
  asserts on `start()`'s outcome and on `db.connection.connect` never having
  been reached, not on any stub having been called; the precondition is a kernel
  lock the test holds, so nothing about it can be satisfied by wiring. Deleting
  step 2b makes `start()` succeed and turns this case red. `flock` is what makes
  it possible in one process at all: the locks belong to the open file
  description, so a second acquisition inside the same process contends exactly
  as a second process does — no subprocess, no `sleep`, no timing (proves the
  guard refuses rather than letting a second bot trade the same account, which
  the `asyncio.Lock`s in `execution.orders` cannot do because they coordinate
  coroutines in one event loop and cannot see another process at all).
- **Closing the descriptor that held the lock — the way the kernel releases it
  on any process death — lets the next `start()` proceed past step 2b (v1.89,
  #22)** (proves a crashed instance does not leave a bot that will not start;
  restarts are routine, and refusing to start is strictly worse than the
  duplicate it prevents when there is nothing left to duplicate).
- **A refusal with no marker file present alerts exactly once, writes
  `${DB_PATH}.instance-lock.refused` containing the `clock.now()` instant, emits
  `startup_failed` with `stage` `instance` and `reason` `INSTANCE_LOCKED`, and
  emits no `startup_ok` (v1.89, #22).** The assertions are the alert count, the
  file's contents and the emitted record — all outcomes an operator or the
  deploy health gate could observe (proves the owner is told the first time, and
  that the refusal is visible to a reader who cannot read Telegram).
- **A second refusal while the marker is fresh emits `startup_failed` again,
  sends no alert, and leaves the marker's recorded instant byte-for-byte
  unchanged (v1.89, #22).** The unchanged instant is the point of the case, not
  a detail of it: rewriting it on every retry would push the age-out forward
  every 30 seconds under the restart loop this rule exists for, so a test that
  only counted alerts would pass an implementation whose latch can never expire
  (proves the storm is suppressed and the window is measured from the first
  refusal).
- **A refusal whose marker records an instant more than 24 hours before
  `clock.now()` alerts again and rewrites the marker (v1.89, #22).** The clock
  is fixed by the test; no `sleep` and no wall-clock reliance (proves the file
  latch ages out, which is the only bound on the accepted risk that a genuine
  second incident weeks later is otherwise quiet).
- **A successful `start()` with a marker file present deletes it (v1.89, #22)**
  (proves the primary reset runs on the path that necessarily executes the
  moment the refusal condition ends — an instance holding the lock is the proof
  that there is only one).
- `start` calls `db.connection.connect` **before** `db.migrations.apply`, and
  `apply` receives `db.connection.shared()` (proves the connection is opened by
  startup rather than at import or inside a repository).
- Importing `app.startup` opens no database file.
- With a budget below every watchlist lot cost, startup **completes** and the
  owner is alerted that nothing is affordable (proves the silent permanent
  `ZERO_LOTS` condition is reported, and — the half that matters — that
  reporting it does not stop the bot protecting open positions).
- With one affordable instrument and one not, no blackout alert is raised and the
  ready alert names the unaffordable ticker (proves a partially reachable
  watchlist is normal and does not burn the alert channel).
- A ticker whose price cannot be read is named as unknown and is counted neither
  affordable nor unaffordable (proves missing data is not silently read as either
  answer).
- When no price can be read for any ticker, the check reports itself
  inconclusive rather than reporting a blackout (proves the check cannot
  manufacture the very finding it exists to detect out of a broker outage).
- A broker failure in step 8a does not raise `StartupError` (proves a diagnostic
  cannot become the reason the bot is down).
- A successful start emits one `startup_ok` log record whose fields are
  `version`, `mode`, `halted` and `adjustments_count`, matching what the ready
  alert reports (proves the deploy health gate has something to observe — it
  greps for exactly this event, and nothing emitted it).
- A `FOREIGN_HOLDING` refusal emits `startup_failed` with `stage` `reconcile`
  and no `startup_ok` (v1.61).
- **After `start()` returns, `/report` from the authorised chat produces a
  report rather than `report unavailable` (v1.88, #36).** The case clears the
  `telegram.commands` builder global to `None` first — it is process-wide, so a
  builder another test installed would otherwise make the case pass with step
  8b deleted — then runs the real `start()`, then drives the real
  `telegram.commands.report` handler with an update shaped the way
  python-telegram-bot shapes one, and asserts on the reply text the owner would
  see: that it is not `report unavailable`, and that it carries the composed
  report's own week header and section text. It asserts an outcome, never that
  a stub was called, and it names no test double as the builder, so a call
  wired to the wrong callable fails it too. The report's benchmark leg is
  mocked at `broker.client`, which is the seam the rulebook names (proves
  startup installs the builder, and not only the `telegram.commands` cases that
  inject one of their own: deleting step 8b left every other `app.startup` case
  green).

## Expected output

- `zarabot/app/startup.py` implementing the contract exactly
- `tests/test_app_startup.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_app_startup.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/app/startup.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
