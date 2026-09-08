# Proposal: #102 S-12 — every §5 table names exactly one owner

**Kind:** spec. AGENTS.md: never write another module's tables. Four tables
lack a §4 “sole owner” sentence:

- `instruments` — no writer in `zarabot/` at all. Cache vs drop is **#46**.
  Rules 12 and the connection-contract sentence that name “instruments cache”
  must match that decision.
- `reconciliations` — owner only in `interfaces.md`; code is `broker.reconcile`.
- `signals` and `daily_snapshots` — §4 section has no “Sole owner” sentence
  (unlike sibling repos).

**Should say:** each `### <table>` in §5 has exactly one §4 “Sole owner of
the `<table>` table.” `instruments`: either `db.instruments` (after #46) or
the table is dropped in a forward migration and the two rule references go.

**Check:** S-06/S-28 style — heading vs owner sentence vs `INSERT`/`UPDATE`
in `zarabot/`.

**Do not:** give `broker.client` SQL. Do not implement the cache in this
amendment.

**Modules:** spec §4/§5. Code writers already match except `instruments`.
