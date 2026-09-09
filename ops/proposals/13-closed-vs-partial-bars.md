# Proposal: #13 F-13 — live vs backtest must see the same bars

**Kind:** brief/spec. Research last. This contradicts fill-model rule 1 at
`technical-spec.md` ~3805 ("bars up to and including the one just closed …
This removes look-ahead completely"). Day D is not just closed at day D's
open. **Rule 1's text is what this amendment changes.**

Both paths **include** today's bar; they differ in its **content**. Live
`get_candles(..., until=now)` can return today's in-progress daily bar
(partial, flickering across polls). `sandbox/exchange.py::_visible` uses
`bar.timestamp <= self._now` (`<=`, not `<`); `_MARKS[0]` is offset 0, so
at the OPEN mark of day D the cursor sits on D and every strategy consumes
the last bar (`momentum` `last = recent[-1]`, `ma_crossover` averages
`closes[-1]`, `rsi_reversion` `reference_price=closes[-1]`, `ml_model`
`last = candles[-1]`). That last bar is day D's **complete** candle,
including its close — a price not knowable at D's open.

Do not write "backtest keeps `timestamp < now` (closed only)" or "opposite
of look-ahead." Do not call the backtest the optimistic side. Fills come
from `_next_bar` (first bar with `timestamp > now`, i.e. D+1's open) at
every mark, so seeing D's close buys **no better fill**. OPEN-mark and
CLOSE-mark decisions produce the same trade at the same price. The cost is
**coherence**: the position opens three marks early with `entry_at` at D's
open, then is exit-evaluated at D's LOW and HIGH against an entry drawn
from D+1's open. Bias direction is not established.

**Should say** one of:

**A (recommended):** drop the in-progress bar in `market.data`; at most one
entry decision per ticker per closed day. `market.data` is shared in the
backtest — `sandbox/backtest.py` patches only `get_candles` *beneath* it
(`candles_for_watchlist` runs both paths) — so a trim there covers live
and backtest. `_visible` may still expose the in-progress bar (timestamp
`== now`) as exchange state for fills via `_next_bar`; it is **not**
permitted to reach a strategy. After the trim, strategies see bars up to
and including the one **just closed**, matching the amended rule 1.

**B:** intraday interval, stated, both paths rebuilt.

**Do not:** exclude the bar in `market.data` while `_visible` still feeds
strategies a different rule, or vice versa. Do not evaluate Option B by
changing only live.

**§3.2:** same `now` + same series → same bars **both** paths (content,
not only timestamps). Option A: a partial-bar signal that dies at the
close produces no trade.

A test that only forbids "a candle timestamped after the decision instant"
is **vacuous** at OPEN: the offending bar's timestamp **equals** the
decision instant while the strategy still reads D's close. Required: at
the OPEN mark of day D, no strategy may observe day D's close. No existing
test asserts this (`tests/test_sandbox_exchange.py` covers fill price from
`_next_bar` only).

**Modules after a choice:** `market.data` and/or `app.loops` (A). `_visible`
stays permitted to list the in-progress bar for the exchange; strategies
must not see its OHLC. Option B adds `sandbox.backtest`. One module per
session.
