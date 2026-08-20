# Zarabot — Business Brief

**Version:** 1.3
**Date:** 2026-08-18
**Status:** Ready for technical spec

**Companion document.** Implementation contracts are in `technical-spec.md`.
When this brief and the spec conflict, the brief takes precedence.

**Versioning.** Versioned manually. A new version is issued when any
architectural decision, security model, risk limit, or scope boundary changes.
Cosmetic edits do not increment the version.

---

## 1. Goal

Zarabot is a personal trading bot that runs unattended on a rented server,
trades a small, deliberately expendable amount of its owner's money on the
Moscow Exchange through the T-Invest brokerage API, and reports on itself once
a week. It executes a handful of simple, well-understood trading strategies —
and, optionally, a machine-learning model trained separately on the owner's own
laptop — trading within hard risk limits it cannot exceed. In ordinary operation
this is expected to produce somewhere around five to ten orders a day, but that
is an expectation about the strategies, not a cap the bot enforces.

Its purpose is education first and income second. The owner wants to be in
contact with their own money: to see strategies win and lose in real conditions,
to learn what the numbers mean, and to build the groundwork for a possible
future income stream. Profit in v1 is a hoped-for side effect, not the measure
of success.

---

## 2. Problem being solved

The owner holds investments that drift up and down and, over time, net out to
roughly nothing. This is not financially painful, but it is passive and
uninstructive — money moves without teaching anything, and there is no feedback
loop between decisions and outcomes.

Zarabot converts a small slice of savings into an active, observable experiment.
Every trade has a recorded reason, every week produces a report that grades what
happened, and the owner learns from real fills, real slippage, and real losses
rather than from articles.

---

## 3. Users

- **Who uses it.** One person: the owner. No other human ever interacts with it.
- **Access model.** Single-user. The bot answers exactly one Telegram chat ID,
  configured at deploy time; messages from any other chat are ignored and logged.
  The owner also has full access to the source code and the server.
- **Platform and interface.** A Telegram bot is the entire user interface. There
  is no web UI, no mobile app, and no public HTTP surface. Day-to-day contact is
  reading alerts and issuing commands from a phone.
- **Supported languages.** English, for all bot messages, reports, and logs.
  Instrument names and broker error text may arrive from T-Invest in Russian and
  are passed through unchanged rather than translated.
- **Chat restrictions.** Private chat only. The bot does not function in groups
  or channels, and does not respond to unknown users.

---

## 4. Architecture overview

The bot is a single long-running Python process containing several cooperating
loops, plus a separate offline research sandbox on the owner's laptop that never
touches production.

```
  ┌────────────────────────────────────────────────────────────────────┐
  │                      SERVER (runs 24/7, Docker)                    │
  │                                                                    │
  │   ┌──────────────────┐                                             │
  │   │ Session guard    │  Is MOEX main session open now (MSK)?       │
  │   └───────┬──────────┘                                             │
  │           │ closed → sleep until next open ──────────┐             │
  │           │ open                                     │             │
  │           ▼                                          │             │
  │   ┌──────────────────┐    API error / timeout        │             │
  │   │ Market data      ├───────────────┐               │             │
  │   │ poller           │               │               │             │
  │   └───────┬──────────┘               ▼               │             │
  │           │ candles      ┌────────────────────────┐  │             │
  │           │              │ Retry w/ backoff       │  │             │
  │           │              │ 3 failures → alert,    │  │             │
  │           │              │ skip cycle, keep alive │  │             │
  │           │              └────────────────────────┘  │             │
  │           ▼                                          │             │
  │   ┌──────────────────┐                               │             │
  │   │ Strategy engine  │  MA crossover / RSI /         │             │
  │   │                  │  momentum / ML model          │             │
  │   └───────┬──────────┘                               │             │
  │           │ signal (ticker, side, size, reason)      │             │
  │           ▼                                          │             │
  │   ┌──────────────────┐   REJECTED                    │             │
  │   │ RISK GATE        ├──────────────┐                │             │
  │   │ • trading halted?│              ▼                │             │
  │   │ • session open?  │   ┌────────────────────────┐  │             │
  │   │ • position cap   │   │ Record rejection +     │  │             │
  │   │ • open positions │   │ reason. No order sent. │  │             │
  │   │ • re-entry wait  │   └────────────────────────┘  │             │
  │   │ • daily loss     │                               │             │
  │   │ • cash available │                               │             │
  │   └───────┬──────────┘                               │             │
  │           │ APPROVED                                 │             │
  │           ▼                                          │             │
  │   ┌──────────────────┐   order rejected by broker    │             │
  │   │ Order executor   ├──────────────┐                │             │
  │   │ (T-Invest API)   │              ▼                │             │
  │   └───────┬──────────┘   ┌────────────────────────┐  │             │
  │           │ filled       │ Record failure, alert, │  │             │
  │           │              │ do NOT retry blindly   │  │             │
  │           ▼              └────────────────────────┘  │             │
  │   ┌──────────────────┐                               │             │
  │   │ Position monitor │──── writes ──►┌────────────┐  │             │
  │   │ stop/target/age  │               │  SQLite    │  │             │
  │   └───────┬──────────┘               │  (1 file)  │  │             │
  │           │                          └─────┬──────┘  │             │
  │           │ daily loss ≥ limit             │         │             │
  │           ▼                                │         │             │
  │   ┌──────────────────┐                     │         │             │
  │   │ KILL SWITCH      │  blocks NEW entries │         │             │
  │   │ halt + alert     │  only. Exits always │         │             │
  │   │ (manual resume)  │  keep running.      │         │             │
  │   └──────────────────┘                     │         │             │
  │                                            │         │             │
  │   ┌──────────────────┐                     │         │             │
  │   │ Telegram bot     │◄── alerts, reports ─┘         │             │
  │   │ (commands + push)│                               │             │
  │   └───────┬──────────┘                               │             │
  │           │ /status /positions /halt /resume ...     │             │
  │           ▼                                          │             │
  │      ┌─────────┐                                     │             │
  │      │  OWNER  │  (single authorised chat ID)        │             │
  │      └─────────┘                                     │             │
  │                                                      │             │
  │   ┌──────────────────┐  Sunday 12:00 MSK ◄───────────┘             │
  │   │ Weekly reporter  │                                             │
  │   └──────────────────┘                                             │
  │                                                                    │
  │   ┌──────────────────────────────────────────────────────────┐     │
  │   │ ON STARTUP / AFTER RESTART                               │     │
  │   │ 1. Fail fast if any required config is missing           │     │
  │   │ 2. Fetch true positions + cash from broker               │     │
  │   │ 3. Reconcile against SQLite; broker is authoritative     │     │
  │   │ 4. Alert owner on any mismatch                           │     │
  │   │ 5. Restore halted/active state from database             │     │
  │   └──────────────────────────────────────────────────────────┘     │
  └────────────────────────────────────────────────────────────────────┘

  Exit path — evaluated every cycle for every open position, and never
  blocked by the risk gate, the cooldown, or an active kill-switch halt:

      stop  −5%     ─┐
      target +10%   ─┼──►  SELL at market  ──►  alert + record which
      age 3 days    ─┘                          trigger fired + start
                                                the 2-hour cooldown

  ┌────────────────────────────────────────────────────────────────────┐
  │            LAPTOP (offline research sandbox — never trades)        │
  │  historical candles → backtests → ML training → exported model     │
  │  file, copied to the server by hand when it earns its place        │
  └────────────────────────────────────────────────────────────────────┘
```

---

## 5. Tech stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.12+ | The owner's language; the entire quantitative and ML ecosystem lives there, and the broker publishes an official Python SDK. |
| Broker API | T-Invest API via its official Python SDK | The owner already banks and invests here; the SDK removes the need to hand-roll authentication, protocol handling, and market-data streaming. |
| User interface | Telegram bot | Reaches the owner's phone anywhere, needs no inbound ports, and needs no authentication system of its own beyond a chat-ID check. |
| Database | SQLite, single file | One user and a handful of trades a day need nothing more. Backed up by copying one file, and downloadable to the laptop for analysis in pandas. |
| Analytics / research | pandas, notebooks on the laptop | Interactive exploration belongs where the owner sits, not on the trading server. |
| ML | scikit-learn or similar, trained offline, exported as a model file | Training never competes with trading for server resources, and no model reaches production without the owner deliberately shipping it. |
| Scheduling | In-process loops and timers | A handful of periodic jobs inside one process is simpler to reason about than an external scheduler, and keeps all state in one place. |
| Runtime | Docker Compose on a rented VPS | One command to deploy, restart policy handles crashes, and the identical image runs on the laptop for testing. |
| Configuration | Environment variables | Keeps secrets out of the repository and makes the live/sandbox switch a deploy-time decision rather than a code change. |
| Logging | Structured logs to stdout, captured by Docker | Standard, greppable, and needs no logging infrastructure to operate or pay for. |

---

## 6. Key concepts

Written for a reader with no trading or ML background.

**Broker API and token.** The broker exposes the account over an internet API.
A *token* is a long secret string that stands in for the owner's identity — it is
equivalent to a password for the account, and anyone holding it can trade with
the money. Tokens come in read-only and full-access forms; trading requires the
full-access form.

**Sandbox versus live.** The broker offers a sandbox account with fake money and
realistic prices. It is used for testing. This bot is configured to trade the
live account from day one, with a deliberately small amount of real money, but
the sandbox remains selectable by configuration for testing changes.

**Candle.** A summary of a security's price over a fixed period — opening price,
highest, lowest, closing, and volume traded. Strategies read sequences of
candles rather than every individual trade.

**Lot.** Exchange instruments are traded in fixed bundles called lots — one lot
might be one share or a thousand. Order sizes must be whole lots, so the bot's
position sizing is always rounded down to a whole number of lots.

**Market order versus limit order.** A market order buys at whatever price is
currently available — it always executes, but the price is not guaranteed. A
limit order specifies the worst acceptable price — the price is controlled, but
it may never execute.

**Slippage.** The gap between the price a strategy assumed and the price actually
paid. It is the main reason a strategy that looks profitable on historical data
can lose money in reality.

**Stop-loss.** A price below the purchase price at which a losing position is
sold automatically, so that it cannot keep getting worse. It is an admission,
decided in advance, that the reason for buying has not worked out — the value of
setting it before the trade is that the decision gets made while nothing is yet
at stake.

**A standing (server-side) stop order.** Rather than watching the price yourself
and selling when it drops, you can lodge an instruction with the exchange in
advance: "if this falls to X, sell." The exchange then watches continuously and
acts even if your own software is switched off. The trade-off is that you now
have an order out in the world that must be withdrawn if you sell for some other
reason first.

**Take-profit.** The mirror of a stop-loss: a price above the purchase price at
which a winning position is sold automatically. Without one, a position has no
defined way to end well — it can only be closed by falling, or by ageing out.

**Gap risk.** Prices do not move continuously. A market that closes at one price
can open the next morning at a very different one, with no trading in between at
the levels skipped. A stop-loss cannot protect against this, because there was
never a moment when the price was available at the stop level. It is the main
risk of holding a position overnight.

**Position, long-only, and no leverage.** A *position* is a holding in one
instrument. This bot is *long-only*: it only buys instruments and later sells
them, and never sells something it does not own (a "short"). It uses no
*leverage* — it never trades with borrowed money — so the maximum possible loss
is the capital allocated to it, and no more.

**Moving-average crossover.** A simple strategy: compare a short-run average
price against a long-run one. When the short average crosses above the long one,
prices are trending up and the strategy buys; the reverse triggers a sell.

**RSI mean-reversion.** RSI is a 0–100 score of how one-sided recent price moves
have been. Very low readings mean heavy recent selling. This strategy buys on
the assumption that such moves tend to snap back.

**Momentum breakout.** Buys when a price pushes above its own recent trading
range, on the assumption that a move which has started tends to continue.

**Backtesting, and why it lies.** Running a strategy over historical data to see
how it would have done. It flatters strategies for two main reasons: *look-ahead
bias*, where the test accidentally uses information that was not yet available at
the time, and *overfitting*, where a strategy is tuned until it perfectly
explains the past and therefore predicts nothing about the future. This is why
the bot's real results, not its backtests, are the measure of a strategy.

**Drawdown.** The drop from a portfolio's high point to its subsequent low — the
practical measure of how bad things got along the way.

**Benchmark.** A do-nothing comparison. Here it is buy-and-hold: what the same
money would have done sitting untouched in the same instruments. A strategy that
earns less than its benchmark has, in effect, cost money to run.

---

## 7. Pre-development verification

Each item must be confirmed before any code is written, because the design
assumes it and a wrong assumption invalidates a component.

1. **A full-access T-Invest token can be issued** and can place, query, and
   cancel a real order on the owner's account.
2. **Sandbox parity.** The sandbox account is reachable with the same code path
   as the live account, so testing exercises the real logic.
3. **Rate limits are documented and measured.** Confirm the actual per-method
   request limits and the exact error returned when exceeded — the polling
   interval and backoff policy depend on real numbers, not guesses.
4. **Historical candles are available** for the watchlist instruments, far enough
   back and at a fine enough interval to backtest the chosen strategies.
5. **The trading calendar is queryable** — session open and close times, weekends
   and Russian market holidays — rather than hardcoded, so the bot does not
   attempt to trade on a closed exchange.
6. **Instrument metadata is retrievable**, in particular lot size, price step,
   and trading status, since position sizing is meaningless without lot size.
7. **Order state is observable after the fact** — a submitted order can be polled
   to final status, so a restart can determine what actually happened.
8. **Telegram delivers to the owner's chat** from the server's network, and
   inline buttons and commands work.
9. **The VPS is provisioned** with Docker, correct system time, and the ability
   to reach both the broker API and Telegram.

---

## 8. Core capabilities

- Track a fixed watchlist of liquid MOEX shares defined in configuration.
- Poll market data during the main trading session only.
- Run several independently enabled strategies over that data:
  moving-average crossover, RSI mean-reversion, momentum breakout, and an
  optional ML model when one is present.
- Turn strategy signals into correctly sized, whole-lot buy orders.
- Enforce every risk limit before an order is sent, and record why a signal was
  rejected when it was.
- Place and monitor real orders automatically, without human approval.
- Close a position automatically when it hits its stop-loss, reaches its
  take-profit target, or has been held for three trading days — the same three
  rules for every position, regardless of which strategy opened it.
- Track open positions, realised and unrealised profit and loss, and cash.
- Reconcile its own view of the account against the broker's on every startup.
- Halt itself when the daily loss limit is breached, and stay halted until the
  owner manually resumes it.
- Accept commands and answer questions over Telegram.
- Push alerts on trades, failures, and halts.
- Produce a weekly written report on its own performance.
- Store every signal, order, fill, rejection, and daily snapshot for later
  analysis on the laptop.

---

## 9. Position lifecycle

Strategies decide only when to **enter**. Every position, whatever opened it, is
managed and closed by the same three rules. The uniformity is deliberate: it
means per-strategy results compare like with like, since no strategy is
flattered or handicapped by having a better exit than another.

| Exit trigger | Condition | Enforced by | Action |
|---|---|---|---|
| Stop-loss | Price at or below 5% under the entry price | **The exchange** | Sells automatically |
| Take-profit | Price at or above 10% over the entry price | The bot | Close at market |
| Maximum age | Position open for 3 trading days | The bot | Close at market, whatever the profit or loss |

**Whichever fires first wins.** The bot evaluates the take-profit and the age
limit on every polling cycle during the session; the stop-loss is watched by the
exchange continuously.

**The stop-loss is a real order held by the exchange.** At the moment a position
opens, a stop-loss order is placed with the broker and left standing until the
position closes. This matters more than it sounds: a stop that lives only inside
the bot protects nothing while the bot is not running. A crash, a failed deploy,
an exhausted VPS or a network outage would otherwise leave every open position
completely unguarded for as long as the problem lasts — and unattended software
on a rented server is down sometimes. With the stop held by the exchange,
protection continues whether or not the bot is alive.

**The take-profit deliberately stays in the bot.** It could also be placed with
the exchange, but every order held externally is one the bot must remember to
cancel when it exits for a different reason, and a missed cancellation means
selling a position twice. A missed take-profit costs an unrealised gain; a missed
stop costs real money. Only the one that protects capital is worth that
coordination risk.

**When the bot exits for its own reasons**, it cancels the standing stop order
first and only then sells. Cancel-then-sell is the fixed order, because the
reverse leaves a live stop order attached to a position that no longer exists.

**The take-profit sits at twice the stop distance**, so one winning trade covers
two losing ones of the same size. That is a choice for symmetry and ease of
reasoning, not a prediction about which setting earns most. The weekly report
shows how often each of the three triggers fired, so real numbers can inform any
later change.

**Maximum age is counted in trading days, not calendar days.** A weekend or a
market holiday does not age a position. The forced exit is placed in the final
fifteen minutes of the third session, so that the full day's price action has had
its chance to reach the target first, and so the order goes out while the market
is liquid rather than at the open.

**Exits are never blocked.** An exit order bypasses the risk gate entirely. The
position cap, the re-entry cooldown, and an active kill-switch halt all apply to
entries only. A risk limit must never be able to trap the bot inside a position
it has decided to leave — a halt means stop making new bets, not stop defending
the ones already placed.

**Exits are market orders.** A limit order that does not fill is not an exit.

**If the protective stop cannot be placed**, the position is not left silently
unguarded. The bot alerts immediately, falls back to watching that position's
stop level itself on every cycle, and marks it in `/positions` so the weaker
protection is visible rather than assumed.

**Overnight holding and gap risk.** Positions are held across session boundaries
until an exit rule fires. The bot observes prices only while the market is open,
so a price that gaps overnight can open well below the stop level; the exit then
happens at whatever the market offers, and the realised loss can be materially
larger than 5%. This is an accepted and understood cost of holding overnight,
not a malfunction. The weekly report records the gap between the intended exit
price and the actual one, so the cost is visible rather than assumed.

**When a position closes**, its instrument enters the two-hour re-entry cooldown,
the result is recorded against the strategy that opened it together with which
trigger fired, and the owner is alerted.

**A failed exit is the one thing worth interrupting a person for.** If an exit
order is rejected by the broker, the owner is alerted immediately and the bot
retries on each following cycle until the position is closed or the owner
intervenes. This is the deliberate exception to the rule that rejected orders
are not resubmitted: a failed entry costs an opportunity, while a failed exit
leaves real money exposed.

---

## 10. Security model

**Threat model.** This is a single-user application on a private server with no
inbound network surface. The realistic risks are not attackers targeting the
system, but leaked credentials, a bug trading more money than intended, and the
server itself being compromised through unrelated neglect.

**Secrets.** The T-Invest token, the Telegram bot token, and the account ID are
supplied as environment variables, are never committed to the repository, never
written to logs, and never included in any Telegram message or report. The
repository contains only an example configuration file with placeholder values.

**Log sanitisation.** Any value originating from a secret is masked before it can
reach a log line or an error message, including inside stack traces and API error
payloads. A leaked token in a log file is equivalent to a leaked password.

**Consequences of token loss.** The T-Invest token is a bearer credential for a
real brokerage account holding real money. Anyone who obtains it can trade or
liquidate the account. If it is exposed, it must be revoked in the broker's
interface immediately; revocation is the only remedy, and the bot will stop
working until a new token is deployed. The Telegram bot token, if leaked, lets a
third party impersonate the bot; the chat-ID restriction means they still cannot
issue commands to the real instance, but they could send the owner convincing
fake alerts.

**Data at rest.** The database holds trade history and P&L — financially
sensitive but not credential-bearing — and is not encrypted, because the
encryption key would have to live on the same server and would protect against
nothing realistic. Protection is by file permissions and by the server not being
shared. Backups inherit the same posture and must not be placed in cloud storage
that the owner would not be comfortable with a stranger reading.

**Isolation.** There is no multi-user data to isolate. The single meaningful
boundary is the authorised Telegram chat ID: commands from any other chat are
refused and logged, so a stranger who discovers the bot cannot query the account
or halt trading.

**Blast radius.** The account the bot trades holds only the allocated capital.
Because the bot is long-only and unleveraged, the maximum loss from any
malfunction is bounded by that amount. Keeping the bot's account separate from
the owner's main savings is the primary security control, and it is a deployment
requirement, not an optional precaution.

**Server hygiene.** SSH by key only, no password login, firewall permitting no
inbound traffic beyond SSH, and automatic security updates. The bot itself opens
no listening ports.

---

## 11. State and history model

- **Authoritative source of truth is the broker.** The database records what the
  bot believes and why; the broker records what is true. On every startup, and
  after any reconnection following an outage, positions and cash are fetched from
  the broker and reconciled. Where they disagree, the broker wins and the owner
  is alerted with the specifics.
- **What persists across restarts.** Open positions with their originating
  strategy, entry price, entry timestamp, and computed stop and target levels —
  everything needed to re-evaluate all three exit triggers after a restart
  without consulting anything but the database and the current price. Also order
  history, every signal including rejected ones with the reason,
  daily P&L snapshots, the halted-or-active trading state, and the per-instrument
  re-entry cooldown timestamps.
- **What does not persist.** In-flight computation, cached candles, and the
  Telegram command context. These are rebuilt on startup.
- **Halt state survives restarts.** If the bot was halted when it stopped, it
  comes back halted. A crash must never be a way to accidentally resume trading.
- **Daily counters reset** at the start of each trading session, in Moscow time.
- **Retention.** Everything is kept indefinitely; the volume is a few megabytes a
  year and the historical record is the point of the project. The database file
  is backed up nightly, with backups retained for thirty days.
- **Conversation state.** The Telegram interface is stateless — each command is
  self-contained, so nothing is lost when the process restarts mid-conversation.

---

## 12. Behaviour under load and limits

**Risk limits.** Every limit below is checked immediately before an order is
sent, and is expressed as a percentage of the allocated capital configured at
deploy time. Limits are enforced in code, not by convention, and a breach means
the order is not sent.

| Limit | Value | Purpose |
|---|---|---|
| Position size on entry | 10% of allocated capital | Every position is the same size, so per-strategy results are directly comparable and no single mistake is expensive. |
| Maximum per position | 20% of allocated capital | Hard ceiling above the entry size, so a sizing or lot-arithmetic bug is caught before it can double an intended position. Enforced at order time only — a holding that grows past it through price appreciation is left alone rather than trimmed. |
| Exit rules: stop −5%, target +10%, max age 3 trading days | See *Position lifecycle* | Bound what any single position can lose and how long it can tie up capital. Exits are never blocked by any other limit on this table. |
| Maximum concurrent open positions | 10 | Bounds total exposure. At 10% per entry this allows the full allocation to be deployed and nothing beyond it. |
| Re-entry cooldown per instrument | 2 hours after closing | Prevents a strategy from looping on the same ticker. Constrains repetition without capping how much the bot may trade in a day. |
| Daily loss limit | 5% of allocated capital | Trips the kill switch. A bad day this size is more likely a bug or a regime change than noise. |
| Leverage | None | Losses cannot exceed allocated capital. |
| Short selling | Not permitted | Removes unbounded loss entirely. |

**Position sizing and full deployment.** Every entry is the same size — 10% of
allocated capital, rounded down to a whole number of lots — and up to ten
positions may be open at once, so the allocation can be fully deployed and never
more than fully deployed. Because entries round down to whole lots, actual
deployment sits slightly under 100%, which is the intended direction of error.
Available cash is checked before every order regardless, so arithmetic drift can
never produce an order the account cannot fund.

**There is no daily order limit.** The number of orders in a day is bounded only
by how often the strategies signal, by the ten-position ceiling, by available
cash, and by the re-entry cooldown. This is deliberate: a good day should not be
truncated by an arbitrary count. The cost of that choice is that a malfunctioning
strategy is caught by the loss limit rather than by an order counter, which is
why the cooldown exists.

**How the two loss floors relate.** The stop-loss is the floor under each
individual holding; the daily loss limit is the floor under the day as a whole.
They are calibrated to comparable severity: ten positions all stopping out on the
same day amounts to roughly the daily limit. A halt therefore signals a broad
adverse move or a systematic fault, rather than one trade going wrong. Full exit
behaviour is described under *Position lifecycle*.

**When the daily loss limit is hit.** The bot halts: no new positions are opened,
and the owner receives an alert naming the loss and the trades that produced it.
A halt suspends entries only — open positions stay fully managed, and their
stops, targets, and age limits keep firing exactly as normal. Nothing is
liquidated wholesale into whatever price happens to be available. It stays halted through restarts until the owner sends the resume
command. This is deliberate friction — it forces a look at what went wrong before
the same behaviour repeats tomorrow.

**When the position limit or available cash is exhausted.** The bot stops opening
new positions until something closes. This is normal operation, not a fault, and
produces one informational message rather than an alert on every subsequent
signal.

**When a signal arrives during an instrument's cooldown.** The signal is recorded
with the cooldown as its rejection reason and no order is placed. Cooldown
rejections are logged and appear in the weekly report — a strategy that is
frequently blocked by cooldown is signalling too often, and that is worth
knowing.

**Broker rate limits.** The polling interval is set conservatively below the
measured limit. On a rate-limit response the bot backs off exponentially rather
than retrying immediately. Sustained rate limiting is treated as a fault and
alerted.

**Broker API errors.** Transient failures are retried with exponential backoff.
After three consecutive failures the cycle is skipped, the owner is alerted once
(not once per retry), and the bot stays alive and keeps trying on the next cycle
rather than exiting.

**Order rejections.** A rejected order is recorded with the broker's reason and
alerted. An entry order is never blindly resubmitted, since the usual causes —
insufficient funds, instrument suspended, bad lot size — will only reject again.
A rejected **exit** is the exception and is retried, as described under
*Position lifecycle*.

**Uncertain order outcomes.** If a submission times out or the process dies
between sending and confirming, the bot does not assume anything. On recovery it
queries actual order state from the broker and reconciles. Double-submitting an
order is treated as the worst possible failure mode and the design avoids it in
preference to the risk of missing a trade.

**Telegram unavailability.** Alerts that fail to send are retried and, if still
undeliverable, written to the log. Telegram being down never blocks or delays
trading logic, and never causes the bot to exit.

---

## 13. Background tasks

| Task | Cadence | Purpose |
|---|---|---|
| Market data poller | Every minute during the main session | Fetches candles for the watchlist; the input to every strategy. |
| Strategy evaluation | After each data poll | Produces signals from current data. |
| Position monitor | Every minute during the session | Checks every open position against all three exit triggers — stop, target, age — and recomputes P&L. |
| Session guard | Continuous | Determines whether the exchange is open, including weekends and holidays, so nothing is attempted against a closed market. |
| Daily rollover | At session open, Moscow time | Resets the daily loss baseline and writes the previous day's snapshot. |
| Nightly backup | Daily, outside session hours | Copies the database file and prunes backups older than thirty days. |
| Weekly reporter | Sunday 12:00 Moscow time | Composes and sends the weekly report. |
| Heartbeat | Daily | One short message confirming the bot is alive, so silence is unambiguous evidence something is wrong. |

---

## 14. Logging and observability

There is no monitoring infrastructure, no metrics stack, and no dashboard. For a
single-user project, Telegram is the dashboard and the log file is the archive.

**Logs.** Structured lines written to stdout and captured by Docker, retained
locally with size-based rotation. Every entry carries a timestamp in Moscow time.
Signals, risk decisions with their reasons, submitted orders, fills, errors, and
state transitions are all logged. Secrets never are.

**Alerts pushed to Telegram immediately.** Every order placed and every position
closed with its result; the kill switch tripping; three consecutive API failures;
any discrepancy found during startup reconciliation; failure to start; and an
order rejected by the broker.

**Deliberately not alerted.** Individual retries, signals rejected by the risk
gate during normal operation, and routine session open and close. A bot that
cries wolf gets muted, and a muted bot is unmonitored.

**The daily heartbeat** exists so that absence of messages is itself a signal. If
no heartbeat arrives, the owner knows to look at the server without having to
remember to check.

**The database is the analytical record.** Anything deeper than the weekly report
is answered by downloading the file to the laptop and querying it there, which is
also where the research sandbox already lives.

---

## 15. Response handling

- **Telegram messages** are kept short enough to read on a phone at a glance. A
  trade alert states instrument, side, size, price, which strategy fired, and
  why, in a few lines.
- **Long responses are truncated, not split.** Position and history listings show
  the most recent or most significant entries and state how many were omitted.
  Telegram's per-message length limit is respected by design rather than
  discovered at runtime.
- **Money is formatted consistently** in roubles with two decimal places, and
  percentages to two decimal places. Every timestamp shown to the owner is in
  Moscow time, labelled as such.
- **The weekly report** is a single message, structured with headings, written in
  plain language. If it would exceed the message limit, the least important
  section is trimmed first and its omission noted.
- **Errors shown to the owner** state what failed, what the bot did about it, and
  whether trading is still running — never a raw stack trace.

---

## 16. Commands

All commands are Telegram commands, accepted only from the configured chat ID.

| Command | What it does |
|---|---|
| `/status` | Whether trading is active or halted, today's P&L, orders placed today, open position count, and time until the next session event. |
| `/positions` | Every open position with entry price, current price, unrealised P&L, and which strategy opened it. |
| `/history` | The most recent trades with their outcomes. |
| `/pnl` | Profit and loss for today, this week, and since inception, against the buy-and-hold benchmark. |
| `/halt` | Immediately stops opening new positions. Existing positions are left untouched. |
| `/resume` | Clears a halt, whether it was manual or triggered by the kill switch. Confirms current risk-limit state before resuming. |
| `/strategies` | Lists enabled strategies and each one's performance to date. |
| `/report` | Generates the weekly report on demand rather than waiting for Sunday. |
| `/help` | Lists the commands. |

There is no command that changes a risk limit, and none that places a manual
trade. Limits are changed by editing configuration and redeploying — a
deliberate speed bump between a bad feeling and a bad decision. Manual trading
belongs in the broker's own app, where it is not confused with the bot's record
of its own behaviour.

---

## 17. Hosting and deployment

- **Platform.** A single rented VPS — 2 vCPU, 2 GB RAM, Ubuntu LTS is sufficient,
  since the workload is a handful of API calls a minute and no training happens
  here. The server must be provisioned before development completes; see
  pre-development verification.
- **Runtime.** Docker Compose, one service, with a restart policy that brings the
  bot back automatically after a crash or a host reboot. The database file and
  backups live on a mounted volume so they survive image rebuilds.
- **Deploy flow.** Pull the repository on the server, build, and bring the stack
  up. Deployment is manual and expected to be infrequent; automating it is not
  worth the plumbing for one user.
- **Deploys happen outside trading hours** whenever avoidable, so that a restart
  never coincides with an open order.
- **Fail-fast configuration.** On startup the bot validates every required
  variable and refuses to start if any is missing, malformed, or contradictory —
  for example a per-position cap and a maximum position count that together
  exceed the allocated capital. It never substitutes a default for a missing
  risk limit and never begins trading on assumed values. A refusal to start is
  loud, in the logs and — if Telegram credentials are among the valid ones — as a
  message.
- **Graceful shutdown.** On a stop signal the bot stops accepting new signals,
  waits for any in-flight order submission to reach a known state, records that
  state, and only then exits. It does not cancel or liquidate positions on
  shutdown — restarts are routine and must not have financial consequences.
- **Server clock accuracy is a requirement**, since session boundaries and
  candle alignment depend on it. Time synchronisation is verified at setup.
- **Timezone.** The server runs in Europe/Moscow, matching the exchange, so no
  conversion errors can creep between market hours and the bot's schedule.

---

## 18. Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TINVEST_TOKEN` | Yes | — | Full-access T-Invest API token. Grants trading rights over real money. |
| `TINVEST_ACCOUNT_ID` | Yes | — | The specific brokerage account the bot may trade. Must hold only the allocated capital. |
| `TRADING_MODE` | No | `live` | `live` or `sandbox`. Selects the broker environment; the code path is otherwise identical. |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Credential for the Telegram bot. |
| `TELEGRAM_CHAT_ID` | Yes | — | The single chat authorised to command the bot. All others are refused. |
| `ALLOCATED_CAPITAL` | Yes | — | Capital the bot may deploy, in roubles. Every risk limit is a percentage of this. |
| `POSITION_SIZE_PCT` | No | `10` | Size of each new position as a percentage of allocated capital. |
| `MAX_POSITION_PCT` | No | `20` | Hard ceiling on any single position as a percentage of allocated capital. |
| `STOP_LOSS_PCT` | No | `5` | How far below entry price a position is closed automatically. |
| `TAKE_PROFIT_PCT` | No | `10` | How far above entry price a position is closed automatically. |
| `MAX_HOLDING_DAYS` | No | `3` | Trading days after which an open position is closed regardless of result. |
| `MAX_OPEN_POSITIONS` | No | `10` | Maximum concurrent open positions. At the default entry size this permits full deployment. |
| `REENTRY_COOLDOWN_MINUTES` | No | `120` | How long an instrument is blocked from re-entry after a position in it closes. |
| `DAILY_LOSS_LIMIT_PCT` | No | `5` | Daily loss, as a percentage of allocated capital, that trips the kill switch. |
| `WATCHLIST` | Yes | — | Comma-separated tickers the bot may trade. Nothing outside this list is ever traded. |
| `ENABLED_STRATEGIES` | No | all rule-based | Which strategies run. Allows disabling one without a code change. |
| `ML_MODEL_PATH` | No | empty | Path to an exported model file. Empty means the ML strategy is off. |
| `POLL_INTERVAL_SECONDS` | No | `60` | Market data polling interval. Must stay below the measured broker rate limit. |
| `DB_PATH` | No | `/data/zarabot.db` | Database file location, on the mounted volume. |
| `BACKUP_DIR` | No | `/data/backups` | Where nightly backups are written. |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity. |
| `TZ` | No | `Europe/Moscow` | Server timezone, matching the exchange. |

---

## 19. Acceptance criteria

Version 1 is complete when all of the following are demonstrably true. None
depends on the bot being profitable.

1. The bot runs unattended for **five consecutive trading days** without manual
   intervention and without an unplanned restart.
2. It has placed **at least one real order on the live account** that filled, and
   the trade appears correctly in both the database and the broker's own records.
3. Every risk limit has been **verified as enforced**: an order that would exceed
   the per-position cap or the open-position count, or that would re-enter an
   instrument still inside its cooldown, is rejected and the rejection is
   recorded with its reason.
4. The **kill switch has been triggered deliberately** in testing: the bot halted,
   alerted, opened no further positions, and stayed halted across a restart until
   `/resume` was sent.
5. The bot has been **restarted mid-session with an open position** and afterwards
   reported that position correctly, having created no duplicate order and lost no
   position. Reconciliation against the broker ran and reported its result.
6. A **deliberately induced broker API failure** caused retries, one alert, and a
   surviving process — not a crash and not a silent stall.
7. The bot **refuses to start** when a required variable is missing, and says
   which one.
8. It **does not trade outside the main session**, verified across a weekend and,
   where the calendar allows, a market holiday.
9. **One weekly report has been delivered** on schedule, containing P&L against
   the buy-and-hold benchmark, per-strategy performance, win rate, the worst
   trade, and written observations.
10. All commands respond correctly, and **a message from an unauthorised chat ID
    is ignored and logged**.
11. Every strategy has been **backtested in the laptop sandbox** on historical
    data, with results recorded — so that live performance can be compared
    against the expectation that motivated the strategy.
12. **No secret appears in any log line**, verified by searching the logs after a
    full run including a failure.
13. **All three exit triggers have been observed firing** — a stop-loss, a
    take-profit, and a maximum-age exit. Each closed its position automatically
    without intervention, recorded the result and the trigger against the
    strategy that opened it, started the instrument's cooldown, and alerted the
    owner.
14. **Exits continue to work while the bot is halted**, verified by halting it
    with a position open and confirming the position is still monitored and still
    exits on its own triggers.
15. **A position held over a weekend** is aged correctly — three trading days, not
    three calendar days — and is not force-closed early.
16. **The protective stop survives the bot being stopped.** With a position open,
    the bot is shut down entirely; the stop-loss order is confirmed still live in
    the broker's own application; and on restart the bot adopts the existing stop
    rather than placing a second one.
17. **A bot-initiated exit cancels the standing stop first.** After a take-profit
    or maximum-age exit, no orphaned stop order remains against the closed
    position.

---

## 20. Out of scope

| Feature | Reason excluded |
|---|---|
| Multi-user access | There is one user. Accounts, permissions, and per-user isolation would be the largest component in the system and would serve nobody. |
| High-frequency trading | The expected rate is a handful of orders a day. Sub-second execution demands a different architecture, colocation, and cost structure, and teaches nothing about finance. |
| Complex strategies — derivatives, pairs trading, arbitrage, portfolio optimisation | The goal is understanding. Strategies whose losses the owner cannot explain defeat the purpose, and options and futures add unbounded loss to a project with a deliberately bounded one. |
| Illiquid and niche instruments | Thin order books mean unpredictable fills and slippage that swamp any strategy edge, and backtests on them are unreliable. |
| Short selling | Introduces theoretically unlimited loss into a project whose entire risk model rests on losses being bounded by allocated capital. |
| Margin and leverage | Same reason: the maximum loss must remain the allocated capital. |
| Web dashboard | Telegram covers monitoring from a phone. A dashboard means a web stack, authentication, and a public surface for a single reader. |
| Automatic model retraining on the server | A model that changes without review can start trading differently for reasons nobody examined. Training stays deliberate and offline. |
| Manual trading through the bot | The bot's record must reflect its own decisions. Discretionary trades belong in the broker's app, where they do not pollute strategy performance data. |
| Multiple brokers | One broker's API is the learning objective. A second adds an abstraction layer with no educational return. |
| Tax reporting | The broker produces the statements that matter for tax. Duplicating them risks producing a confidently wrong number. |
| News and sentiment analysis | An entire data-acquisition and NLP problem in its own right, with no clean way to validate that it helped. |
| Automated deployment pipeline | Deploys are infrequent and manual. CI plumbing would be built before the bot exists and maintained for one user. |
| Backtesting user interface | Backtests run in notebooks on the laptop, where the tooling already exists and is better. |
| Driving the bot through the broker's MCP server | The broker offers an MCP server that lets an AI agent trade in natural language. It is the wrong tool for this loop: the strategies are exact calculations that gain nothing from a language model, an MCP call cannot be replayed in a backtest, and the interface is unlikely to expose the idempotency key the crash-recovery design depends on. The broker also disclaims responsibility for AI-driven losses and reserves the right to withdraw access. **It is, however, a good research tool**: connecting a desktop AI client to it with a **read-only** token is a supported way to ask questions about the portfolio, and requires no code and no change to this project. |

---

## 21. Open questions

None. All decisions required to begin the technical spec are resolved. For the
record, the following were open during drafting and are now settled:

| Question | Resolution |
|---|---|
| Approve trades manually, or execute automatically? | Automatic execution within hard risk limits. Approval friction was judged not worth it at this trade rate. |
| Sandbox first, or real money immediately? | Real money from day one, with a small deliberately expendable allocation. Sandbox remains available by configuration for testing. |
| Daily loss limit | 5% of allocated capital. |
| Maximum position size | 20% of allocated capital. |
| Capital amount | Not fixed in this document. Set as configuration at deploy time; every limit is a percentage of it. |
| Interface | Telegram only. |
| Database | SQLite. |
| ML training location | Laptop only; models are exported and shipped manually. |
| Language | English. |
| Server | A VPS still to be rented; provisioning is a pre-development step. |
| Position size on entry | 10% of allocated capital, identical for every trade. |
| What closes a losing position | A fixed stop-loss 5% below entry, checked every polling cycle. |
| Maximum concurrent positions | 10, so the whole allocation can be deployed. |
| Daily order cap | None. Removed deliberately; a good day should not be truncated by a counter. |
| Runaway-loop protection | A 2-hour per-instrument re-entry cooldown, replacing the order cap. |
| Who decides exits | The bot, uniformly. Strategies generate entry signals only. |
| Take-profit | 10% above entry — twice the stop distance. |
| Maximum holding period | 3 trading days, then close at market regardless of result. |
| Exits while halted | They continue. A halt blocks entries only. |
| Overnight positions | Held until an exit rule fires. Gap risk accepted and documented. |
| Where the stop-loss lives | With the exchange, as a standing order, so protection survives the bot being down. |
| Where the take-profit lives | In the bot, to avoid coordinating two external orders per position. |
| Broker MCP server | Out of scope for the bot; supported as a read-only research tool. |
