# Proposal: #28 slippage / liquidity — product, not an execution tweak

**Kind:** spec + brief. Money path. Do not auto-amend. Do not implement a
policy (keep vs unwind) in `execution.orders` first.

Every entry is `ORDER_TYPE_MARKET`. The gate sizes from `signal.reference_price`
(last close; under #13 that may be an in-progress bar). The fill is accepted
if it filled. Stop/target are derived from the **fill**, so the **percentage**
risk is preserved; the **rouble** size is not what the gate approved.

Stops and bot exits should **stay market**. A limit stop that does not fill
in a falling market is worse than slippage.

**Options (human picks; recommend 1+2, not 3 yet):**

1. **Post-fill guard.** Compare `filled_price` to `reference_price`. Beyond
   a configured tolerance: alert. Whether to keep or immediately sell is a
   **stated policy**. Unwind is a second trade (cancel stop, sell, cooldown) —
   it needs the same write-then-send rules. Do not invent "keep unless X%".
2. **Pre-trade liquidity.** New `RejectionReason` in `risk.gate` (pure:
   volume/spread must be inputs, not a broker call inside the gate). Closest
   match to brief §20 excluding illiquid names.
3. **Limit orders.** Changes recovery (`SUBMITTING` / unfilled rest). Spec
   `execution.orders` first. Not this amendment unless the brief wants it.

**Must NEVER stays:** `confirm_margin_trade=True` is still never passed.
Never estimate commission. Never resubmit an entry.

**§3.2 after numbers exist:**

- Fill beyond tolerance → the chosen event/alert (and keep or unwind as
  specified).
- Ticker failing the liquidity inputs → rejected with the named reason.
- Existing fill/recovery tests stay green.

**Modules, after the brief:** `config` (tolerance / thresholds), `risk.gate`
(liquidity), `execution.orders` only for the post-fill alert/unwind path.

**Stop:** do not unwind a fill in this module because a test fixture showed
5% drift. That is a new trade.
