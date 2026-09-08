# Proposal: #13 F-13 — live vs backtest must see the same bars

**Kind:** brief/spec. Research last. Live `get_candles(..., until=now)` includes
today's in-progress daily bar; entries re-run every poll. Backtest keeps
`timestamp < now` (closed only). Live can trade a 10:05 flicker the backtest
never sees. Opposite of look-ahead; still disqualifies comparison.

**Should say** one of:

**A (recommended):** drop the in-progress bar; at most one entry decision per
ticker per closed day. Matches daily-bar strategies and the backtest.

**B:** intraday interval, stated, both paths rebuilt.

**Do not:** exclude the bar in `market.data` while the backtest still
documents a different rule, or vice versa. Do not evaluate Option B by
changing only live.

**§3.2:** same `now` + same series → same bars both paths. Option A: a
partial-bar signal that dies at the close produces no trade.

**Modules after a choice:** `market.data` and/or `app.loops` (A), plus
`sandbox.backtest` if B. One module per session.
