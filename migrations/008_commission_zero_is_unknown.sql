-- A zero commission recorded at fill was never a measurement (#246, spec v1.91).
--
-- `broker.client._executed_commission` read `executed_commission` off the order
-- state and wrote `Decimal(0)` when the broker reported zero there, which it
-- does on every fill: 22 of 22 on the live account, against 22 fee operations
-- totalling 14.67 for the same trades. `db.orders.list_missing_commission`
-- selects `commission IS NULL`, so the backfill written to recover a late
-- commission has never been shown one of those rows. It ran daily, found
-- nothing, and reported success, and every realised P&L was gross.
--
-- Recording `None` from now on fixes the next order and does nothing for the
-- rows already on disk. This is the history half.
--
-- Nothing measured is lost. The *measured* zero — a trade the operations feed
-- shows with no fee child — is a concept v1.91 introduces, and the only code
-- that can write one is the backfill running after this migration. Every zero
-- in the column predates it and is an absence.
--
-- Scoped to FILLED rows and to a numeric zero. `CAST(... AS NUMERIC)` so that
-- every spelling a serialised Decimal may have ('0', '0.00', '0E-9') is caught
-- and nothing else is.
--
-- This alone does not repair the P&L. The seven closed positions keep their
-- gross `realised_pnl` until one operator-run `ops.commissions.backfill` over
-- the full account history recomputes them. Procedure in `ops/RUNBOOK.md`.

UPDATE orders
   SET commission = NULL
 WHERE status = 'FILLED'
   AND commission IS NOT NULL
   AND CAST(commission AS NUMERIC) = 0;
