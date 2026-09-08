# Proposal: #111 S-21 — PORTFOLIO_EXPOSURE can bind after fill slippage

**Kind:** spec. The “cannot bind on a self-sized portfolio” proof assumes
each open cost ≤ one budget. Sizing uses pre-submit price; fill is market;
`open_cost` uses entry. Fill above size → cost > one budget. Related: F-28.

**Should say:** the guarantee is at **sizing** price. Fill slippage can make
`PORTFOLIO_EXPOSURE` bind on the next signal; that is not a gate bug.

**Do not:** treat a binding exposure after a rich fill as a defect to “fix”
by ignoring `open_cost`.

**Modules:** spec `risk.gate` / `risk.sizing` paragraphs only.
