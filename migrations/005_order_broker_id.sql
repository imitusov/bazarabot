-- A row describing an execution the exchange performed on the bot's behalf is
-- filed under an idempotency key the bot invented and the broker has never
-- seen, so get_order_state on it can only ever return OrderNotFound and a
-- commission that lands late is lost forever (#8). broker_order_id is the
-- broker's own identifier, where the bot knows it.
--
-- commission_alerted_at gives the "commission still unknown" alert a terminal
-- state. Without it the same row alerted on every backfill run — daily, and
-- again before every weekly report — and an alert that repeats forever is
-- equivalent to no alert.
--
-- Both nullable, no default, no backfill: the live database holds zero orders.

ALTER TABLE orders ADD COLUMN broker_order_id TEXT NULL;
ALTER TABLE orders ADD COLUMN commission_alerted_at TEXT NULL;
