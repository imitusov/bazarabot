# Proposal: #104 S-14 — `refresh(days)` is the signature; step 5 must pass `days`

**Kind:** spec prose. Signature: `async refresh(days: int) → None`. v1.68
said no `now` and callers call without one — that is false for `days`.
Startup step 5 writes `refresh()`. Code: `refresh(_SCHEDULE_DAYS)`.

**Should say:** callers pass `days`. No `now` (clock inside). Step 5 matches.

**Do not:** change the signature to zero arguments.

**Modules:** spec `market.session` + `app.startup` step 5. Code already correct.
