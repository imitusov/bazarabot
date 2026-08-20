# execution/ — where money moves

The rules in this directory are not style preferences. Every one of them exists
because violating it costs real money on a live account.

## Ordering is the contract

1. **Write, then send.** `db.orders.record_submitting()` must complete *before*
   `broker.client.post_market_order()` is called with that key. Reversing these
   two lines is a critical defect: it creates a window where the broker has an
   order and the database has no record of it, and the next cycle places a
   second one.
2. **Cancel the stop, then sell.** Any bot-initiated exit cancels the standing
   stop order and demotes the position to `LOCAL` before submitting the sell.
   Selling first leaves a live stop attached to a position that no longer
   exists.
3. **Position row, then stop order.** The position exists before its protective
   stop is placed, and is inserted as `LOCAL`. It is promoted to `EXCHANGE` only
   once the stop is confirmed standing.

## Never

- **Never pass `confirm_margin_trade=True`.** No condition makes this correct.
  It permits losses exceeding the allocated capital — the single failure the
  brief promises cannot happen.
- **Never resubmit an entry order.** Recovery is by querying with the
  idempotency key. An entry rejection is terminal.
- **Never let an exit be blocked** by halt state, cooldown, or any risk limit.
- **Never acquire a lock without guaranteed release** on success, exception, and
  cancellation. Use an async context manager.

## Exits are the exception to the no-retry rule

A rejected *entry* is terminal — the cause will reject again. A rejected *exit*
is retried every cycle until the position closes, and alerts immediately. A
failed entry costs an opportunity; a failed exit leaves money exposed.

## Coverage

**95%.** A missed branch here is a financial defect, not a coverage statistic.
