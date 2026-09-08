# Proposal: #46 `instruments` table needs an owner and a cacheability rule

**Kind:** spec. The table exists (`migrations/001_initial.sql`) with
`refreshed_at`. `Instrument` already has `refreshed_at`. No module writes the
table. `app.loops` and `pnl.benchmark_return` call `get_instrument` per ticker
per cycle.

**Wrong / missing:** §5 lists `instruments` with no owning module. Caching
`trading_status` would let `risk.gate` approve an instrument that halted this
cycle (`INSTRUMENT_NOT_TRADING`). Lot size / min increment / currency are
effectively static.

**Should say:**

1. New `zarabot/db/instruments.py` owns the table: `shared()`, writes inside
   `transaction()`, no SQL elsewhere.
2. **Cacheable:** `lot`, `min_price_increment`, `currency` (and names/figi)
   within a named freshness window.
3. **Not cacheable, or window so short it cannot matter:** `trading_status`.
   Prefer a live read for that field so a halt is visible on the cycle it
   happens. Say which.
4. `broker.client.get_instrument` remains the refresh path; the repository
   does not call the broker (ownership). Who calls refresh: `app.loops` or
   `broker.client` wrapping the cache — pick one. Prefer `app.loops` asking
   the repository then the client, so `broker.client` stays the only network
   owner.

**Do not:** cache `trading_status` for a day. Do not drop the table in the same
change as adding the repository (that is a different decision).

**§3.2:**

- Ten-ticker cycle within the window: far fewer than ten broker
  `get_instrument` calls.
- Past the window: refetch.
- Instrument that halts mid-window: `risk.gate` still rejects
  `INSTRUMENT_NOT_TRADING` on that cycle.

**Modules to re-run, lowest first:** new `db.instruments` task, then
`broker.client` only if the contract moves the cache there, then `app.loops`
/ `pnl` callers.

**Stop:** do not implement the repository until the spec names the
`trading_status` rule. A cache that lies about halt is a money bug.
