# Proposal: #99 S-09 — gate §3.2 must list live reasons only

**Kind:** spec. `risk.gate` 95% module. Six lines in one §3.2 block: POSITION_CAP
is described as removed, then the “every branch, one test each” list still
names **position cap**. `PORTFOLIO_EXPOSURE` replaced it (v1.30).

`BROKER_LOT_LIMIT` is on the enum with **no producer** — `grep -rn
BROKER_LOT_LIMIT --include=*.py` returns only `models.py:43` and an enum
membership assertion, and `risk/gate.py` produces the other nine reasons and
never it. It is inert by §1398's own definition ("a value the schema would
still accept but no code can produce is inert"), which sits two lines above the
paragraph at §1400 calling it live.

The behaviour §1400 ascribes to it — "the broker refusing the size outright,
its maximum for the account is zero lots" — **is implemented**, in
`execution/orders.py:404-415`: `allowed = await get_max_lots(...)` then
`if lots <= 0` raising `OrderRejected("max lots is 0")`, free text, no enum.
That is the right module for it, because a broker call cannot live in
`risk.gate` — the rulebook's pure list, and `gate.py` imports only `config`,
`models` and `risk.sizing`. Its disposition is a separate issue, not this one.

**Should say:** the enumerated reasons are exactly the live
`RejectionReason` set: halted, session closed, `PORTFOLIO_EXPOSURE`, max
positions, cooldown, insufficient cash, zero lots, instrument not trading,
duplicate ticker. No `POSITION_CAP`.

Enumerate the nine the gate produces, not the enum. Keying the list to
`RejectionReason` membership would demand a per-branch test for every member
including inert ones, which on a 95%-coverage module leaves a regenerated task
either carrying an unsatisfiable test contract or inviting an agent to write a
new rejection branch under cover of a prose amendment.

**Do not:** add a test that constructs `max_position_pct` — `config.load()`
rejects that state.

**Modules:** spec §3.2 `risk.gate` only. Tests cover the nine reasons the gate
produces; amend so a regenerated task cannot demand the dead one.
