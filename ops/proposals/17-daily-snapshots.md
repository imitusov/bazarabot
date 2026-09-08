# Proposal: #17 fill `daily_snapshots` at session close, not only at open

**Kind:** spec + wiring. `db.snapshots.write_daily` already upserts. The row is
seeded at session open from `app.loops` with zeros and `closing_equity=None`,
and **never written again**. `/status` then reports a confident `0.00`.

**Wrong / missing:** `app.loops` and `pnl` contracts never assign a session-close
(or last-cycle) write of `closing_equity`, real `cash`, day's `realised_pnl`,
mark-to-market `unrealised_pnl`, `open_positions`, `orders_placed`, or
`benchmark_value`. `write_daily` is specified as an upsert, but no §4 module is
told to call it after the open seed. `db.orders` has no "count for Moscow date"
reader.

**Should say:**

1. **Session open** (already intended, `#9`): write `opening_equity` once; do
   not overwrite an existing row's opening figure on restart.
2. **Session close / last cycle of the trading day:** upsert the same
   `trade_date` with `closing_equity`, `cash` (broker cash, not equity),
   `realised_pnl` from that day's closed positions, `unrealised_pnl` from open
   mark-to-market, `open_positions`, `orders_placed`, `benchmark_value`.
3. `db.orders` grows a dated count (or `db.snapshots` is allowed one read of
   orders through the owning repository — not raw SQL). Name the owner.
4. `/status` reads those fields; zeros are only valid when the day truly had
   none.

**Do not:** keep treating `cash=equity`. Do not invent a second snapshot table.
Do not compute Sharpe in this change.

**§3.2 that would have caught it:**

- After a simulated day with one closed trade, the row has non-null
  `closing_equity` and `realised_pnl` matching that close.
- A mid-day restart does not reset `opening_equity`.
- `/status` P&L matches closed + open mark-to-market for that Moscow date.

**Modules to re-run, lowest first:** `db.orders` (count), `db.snapshots` only if
the record grows fields, `app.loops` (close write), then `telegram.commands`
`/status` if its contract is amended to require the filled row.

**Stop:** this is more than one module. Do not implement the close write in
`app.loops` until `orders_placed` has a specified owner.
