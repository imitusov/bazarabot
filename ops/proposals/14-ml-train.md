# Proposal: #14 F-14 — ML train/score/horizon before it can fire

**Kind:** research. `sandbox/train` + `strategies.ml_model`. Four defects:
unscaled features into L2 logistic; no class weight; accuracy on an
imbalanced label; calendar-day horizon vs live `trading_days_between`.
Combined: `predict_proba` rarely clears 0.60; `/strategies` still shows it
enabled; fold scores look fine.

**Should say:** `Pipeline(StandardScaler, LogisticRegression)`;
`class_weight="balanced"` (or resample); score precision/recall/lift **at
0.60** and fail the run if predicted-positive rate is ~0; `_label` uses
trading days via `clock`; optional model hash on the position row (schema).

**Do not:** enable ML in live until train fails loud on a zero-positive
model. Do not estimate labels with calendar days.

**Modules:** `sandbox.train` first (laptop), then `strategies.ml_model` if
feature order/scaling must match the pipeline. Schema last if versioning.
