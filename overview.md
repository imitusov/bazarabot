# Zarabot — Overview

**Status:** non-normative. This document is an on-ramp, not a source of truth.
Nothing is generated from it and no contract lives here. When it disagrees with
anything else, it is the one that is wrong, in this order:
`business-brief.md` → `technical-spec.md` → this file.

Read this to understand what the bot is and why it is built this way. Read the
brief for intent and scope boundaries, the spec for contracts, signatures and
error rules, `dependency-order.md` for build sequence, `interfaces.md` for what
already exists.

---

## What it is

A personal trading bot. One long-running Python process on a rented VPS trades a
small, deliberately expendable amount of its owner's money on the Moscow
Exchange through the T-Invest API, and reports on itself once a week. It runs a
handful of simple strategies — moving-average crossover, RSI mean-reversion,
momentum breakout, and optionally an ML model trained offline — inside hard risk
limits it cannot exceed.

**Purpose is education first, income second.** Profit in v1 is a hoped-for side
effect, not the measure of success. The owner is buying a feedback loop between
decisions and outcomes: real fills, real slippage, real losses.

One user, one Telegram chat ID, no web UI, no inbound ports. Expected rate is
roughly five to ten orders a day — an expectation about the strategies, not a
cap the bot enforces.

---

## The shape of a cycle

Every minute during the MOEX main session:

```
session guard → market data poll → strategies → RISK GATE → order executor → SQLite
                       ↓ 3 failures                  ↓ rejected
                  alert once, skip cycle,       record reason,
                  stay alive                    no order sent
```

Running alongside it, and independent of it:

- **Position monitor** — every open position against all three exit triggers.
- **Kill switch** — daily loss ≥ 5% halts *new entries*; exits keep running.
- **Telegram** — commands in, alerts and the Sunday report out.
- **Startup reconciliation** — fetch true positions and cash from the broker,
  reconcile against SQLite, alert on mismatch, restore halted state.

A separate offline sandbox on the owner's laptop does backtests and ML training.
It never trades and is never imported by `zarabot/`. A model reaches the server
only by the owner copying it there.

---

## The money invariants

These are the parts where a bug costs money rather than producing a stack trace.

**Limits**, all as a percentage of allocated capital, checked in code
immediately before an order is sent:

| Limit | Value |
|---|---|
| Position size on entry | 10%, identical every trade, rounded down to whole lots |
| Total portfolio exposure | 100% |
| Maximum concurrent positions | 10 |
| Re-entry cooldown per instrument | 2 hours after close |
| Daily loss limit | 5% → kill switch |
| Leverage / short selling | None / not permitted |

Ten positions at 10% deploys the whole allocation and nothing beyond it. Rounding
down means actual deployment sits slightly under 100%, which is the intended
direction of error. Long-only and unleveraged means the maximum possible loss
from any malfunction is the allocated capital — that bound is the promise the
whole risk model rests on.

**Exits are uniform.** Strategies decide only when to enter. Every position,
whatever opened it, is closed by the same three rules — stop −5%, target +10%,
maximum age 3 *trading* days — so per-strategy results compare like with like.
Whichever fires first wins. Exits are market orders; a limit order that does not
fill is not an exit.

**The stop-loss lives at the exchange; the take-profit lives in the bot.** A stop
that lives only inside the bot protects nothing while the bot is down, and
unattended software on a rented VPS is down sometimes. The take-profit stays
local because every externally held order is one that must be cancelled on any
other exit, and a missed cancellation sells a position twice. A missed
take-profit costs an unrealised gain; a missed stop costs real money. Only the
one that protects capital is worth the coordination risk.

**Exits are never blocked.** The exposure ceiling, the cooldown and an active
halt apply to entries only. A halt means stop making new bets, not stop defending
the ones already placed. Nothing is liquidated wholesale on a halt, and the halt
survives restarts until the owner sends `/resume`.

**Order of operations is fixed.** Write the intent to the database, then call the
broker — that is what makes a crash mid-submission recoverable. Cancel the
standing stop, then sell. Never resubmit an entry: on an uncertain outcome, query
by idempotency key. The one exception is a **failed exit**, which is retried every
cycle and alerted immediately, because a failed entry costs an opportunity while
a failed exit leaves real money exposed.

**Accepted costs, documented rather than fixed.** Positions are held overnight,
so a gap can open below the stop and the realised loss can exceed 5%. If the
protective stop cannot be placed at all, the bot alerts, falls back to watching
that level itself, and marks the position in `/positions` so the weaker
protection is visible rather than assumed.

**The account is the bot's alone.** If the broker reports a holding the bot has
no record of, it refuses to start, naming the tickers — or reports and ignores
them forever under `ALLOW_FOREIGN_HOLDINGS=true`. A bot that will not start is an
inconvenience noticed immediately; one that quietly liquidates a long-term
holding is discovered afterwards.

---

## Security posture

The realistic risks are not attackers: they are a leaked token, a bug trading
more than intended, and server neglect.

- The T-Invest token is a bearer credential for a real account. Leaked, the only
  remedy is revocation. It never enters a log line, a message, a report, or a
  stack trace — including inside broker error payloads.
- Secrets arrive as environment variables; the repo holds only placeholders.
- The database is not encrypted — the key would sit on the same server — and is
  protected by file permissions. Backups inherit that posture.
- The one meaningful boundary is the authorised chat ID. Other chats are refused
  and logged.
- **Keeping the bot's account separate from the owner's savings is the primary
  security control**, and a deployment requirement rather than a precaution.

---

## What "done" means

Seventeen acceptance criteria in brief §19, none of which depends on the bot
being profitable. The shape of them: five unattended trading days; one real
filled order reconciled against the broker; every risk limit observed rejecting
something; the kill switch tripped deliberately and surviving a restart; a
restart mid-session with an open position creating no duplicate; an induced API
failure producing retries and one alert rather than a crash; all three exit
triggers observed firing; exits still working while halted; a position aged
correctly across a weekend; the stop surviving a full shutdown and being adopted
rather than duplicated on restart; and no secret in any log line after a run that
included a failure.

---

## Deliberately not built

Multi-user access, HFT, derivatives and pairs trading, illiquid instruments,
shorting, margin, a web dashboard, on-server retraining, manual trading through
the bot, multiple brokers, tax reporting, news and sentiment, CI/CD, a backtest
UI. Reasons in brief §20; most reduce to one of three: there is one user, the
loss bound must stay intact, or it teaches nothing.

The broker's MCP server is out of scope for the trading loop — an MCP call cannot
be replayed in a backtest and is unlikely to expose the idempotency key recovery
depends on — but is a good **read-only** research tool, requiring no code here.
