# Proposal: #69 O-15 is operator evidence, not a CI close

**Kind:** deploy / live. This agent has no VPS, no live DB, and must not
place orders. CI green on producer PRs is **not** brief §19.

**Checklist stays on the issue.** Record here only what a cloud agent cannot
do and must not fake:

- Deploy window 02:00–05:00 MSK, no in-flight orders.
- One `startup_ok`; `.deployed-digest` matches the image.
- SQLite `schema_version` equals current migrations, not a stale v2.
- `zarabot-health.service` → `ops/health` aggregates only.
- Open-session `entry_funnel.py` inside the app image.
- Health-export counts vs DB (`signals`, heartbeats, reconciliations,
  `trading_days`).
- Failure drills without extra live money.
- Grep rotated logs for tokens and account ids (rule 19 / brief acceptance 12).
- Map brief §19 have/have-not with artefact paths.

**Do not:** mark O-15 complete from GitHub Actions. Do not paper over
missing `startup_ok` or schema lag. Do not trade to populate the funnel.

**Done when:** every checkbox has pass / fail / blocked-with-reason.
Zero trades with a populated funnel is allowed; silence is not.
