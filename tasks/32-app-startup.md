# Task 32/40: Implement `zarabot/app/startup.py`

## Product context

Fixed startup ordering: config, logging, connection, migrations, strategies, session, order recovery, reconciliation, halt state, ready alert. 70% coverage.

## Build order position

Module **32** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/app/startup.py`

**`async start() → AppContext`**

Fixed ordering; each step completes before the next begins:
1. `config.load()` — abort on failure before anything else, including any network call.
1b. Write `SSL_TBANK_VERIFY` into the process environment from
   `config.ssl_tbank_verify`. This must precede every broker call; a channel
   created before it is set fails its TLS handshake.
1c. **When `ssl_tbank_verify` is false, alert the owner before the first broker
   call**, saying that certificate verification is disabled on the connection
   carrying the trading token. `config` logs it; a log line on a server nobody
   is watching is not a security control. The alert must never contain the
   token.
2. `logging_setup.configure()`.
3. `db.connection.connect(config.db_path)`, then
   `db.migrations.apply(db.connection.shared())`. The connection is opened here —
   not at import, and not inside a repository — and `apply` receives the shared
   connection rather than opening a second one.
4. `strategies.registry.enabled()`, including model load if configured.
5. `market.session.refresh()`.
6. `execution.orders.resolve_unfinished()`.
7. `broker.reconcile.reconcile()`, then apply its remedies via
   `execution.orders`: re-protect unprotected positions, cancel orphaned stops,
   replace mispriced ones, and **resolve duplicates — for a `STOP_DUPLICATE`
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
   `CLOSED_EXTERNALLY`, `ADOPTED`, `LOTS_ADJUSTED` and `FOREIGN_HOLDING` in the
   set of types this step recognises without acting on, precisely so it does not
   trip the "adjustment types this build cannot act on" alert, which is reserved
   for a report the executor genuinely does not understand.

   Its consequence is named here rather than left to be discovered: the position
   stays open, so if it was `EXCHANGE` the same report will carry `STOP_MISSING`
   for it and this step will try to place a stop against shares the account does
   not hold. The broker refuses, the position stays `LOCAL`, and the owner is
   alerted — the degrade path rule 23 already defines. That is noisy and correct;
   the alternative was a fabricated exit price written permanently into the trade
   history.

7b. **Refuse to start on a `FOREIGN_HOLDING` adjustment**, unless
   `config.allow_foreign_holdings` is true. Raise `StartupError` naming every
   ticker reported, after alerting. The account is the bot's alone (brief v1.8),
   and a holding the bot does not recognise means either that someone traded in
   it by hand or that local state is wrong — and the bot cannot tell which. When
   the flag is set, the holdings are named in the ready alert instead and are
   never traded: no stop is placed, no exit is evaluated, no sale is made.
   Refusing is the correct failure direction. The alternative failure is selling
   something the owner chose to hold, at a price they did not choose.
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

   **This step never raises `StartupError`, and never prevents startup.** An
   unaffordable budget stops *new entries only*. Refusing to start would
   additionally abandon every open position — no exit evaluation, no stop
   management, no `MAX_AGE` — converting a benign no-op into an unmanaged holding
   with real money in it. The bot must keep running to protect what it holds.

   It runs after step 7 so reconciliation has settled position truth first, and
   before step 9 so the finding can be folded into the ready alert. A broker
   failure in this step is alerted and startup continues; this is a diagnostic,
   and it must never be the reason the bot is not running.

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

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

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

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Startup with valid config, a reachable broker and a clean database completes
  and reports ready (happy path).
- Invalid config aborts before any broker call is made (proves fail-fast
  ordering).
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
