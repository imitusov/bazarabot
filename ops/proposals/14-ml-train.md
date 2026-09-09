# Proposal: #14 F-14 — ML train/score/horizon before it can fire

**Do not enable ML in live until train fails loud on a zero-positive
model.** `/strategies` lists the strategy enabled either way, including
when nothing can clear `CONFIDENCE_THRESHOLD = 0.60`. That is a live-money
silence; it belongs here, not as a trailing note.

**Kind:** research. `sandbox/train` + `strategies.ml_model`. Four defects:
unscaled features into L2 logistic; no class weight; accuracy on an
imbalanced label; calendar-day horizon (`timedelta(days=horizon_days)`)
vs a trading-day series. Combined: `predict_proba` rarely clears 0.60;
`/strategies` still shows it enabled; fold scores look fine.

**Should say:** `Pipeline(StandardScaler, LogisticRegression)`;
`class_weight="balanced"` (or resample); score precision/recall/lift **at
0.60** and fail the run if predicted-positive rate is ~0; `_label` uses a
**bar-index offset** on the daily series it already iterates (the series
is trading days by construction). Do **not** call
`clock.trading_days_between`: that needs a `TradingCalendar` for a
historical window, and §2.1 records **Trading schedule, past — Not
obtainable** (`INVALID_ARGUMENT` / 30003). `db.trading_days` does not
rescue it (forward observations only; empty for a multi-year laptop
window; `sandbox/` does not open the live database). Optional model hash
on the position row (schema).

**Do not:** estimate labels with calendar days. Do not send an implementer
at a schedule endpoint that will reject them.

**Modules:** `sandbox.train` first (laptop), then `strategies.ml_model` if
feature order/scaling must match the pipeline. Schema last if versioning.
