# Proposal: #102 S-12 — every §5 table names exactly one owner

**Kind:** spec. AGENTS.md: never write another module's tables.
`scripts/ci/check_docs.py` already encodes this: `KNOWN_UNOWNED_TABLES`
waives `instruments`, `schema_version`, `daily_snapshots`, `halt_state`,
`reconciliations`. `stale_tables` exits 1 if `instruments` is deleted from
§5 without editing that allowlist — dropping the table in a forward
migration without the gate edit hard-fails `make check`.

Do not list `signals` as unowned: `db.signals` writes it and the gate
does not waive it.

Tables the gate currently waives:

- `instruments` — no writer in `zarabot/` at all. Cache vs drop is **#46**.
  Rules 12 and the connection-contract sentence that name “instruments cache”
  must match that decision.
- `schema_version` — §4 says “schema creation”, names no table; written by
  `db.migrations`.
- `daily_snapshots` — §4 section has no “Sole owner” sentence.
- `halt_state` — §4 says “the halt flag”, names no table; written by
  `state.halt`.
- `reconciliations` — owner only in `interfaces.md`; code is
  `broker.reconcile` (direct SQL — an exception to “access only through the
  owning repository”).

**Should say:** each `### <table>` in §5 has exactly one §4 “Sole owner of
the `<table>` table.” `instruments`: either `db.instruments` (after #46) or
the table is dropped in a forward migration **and** `KNOWN_UNOWNED_TABLES`
in `scripts/ci/check_docs.py` is edited in the same amendment.

**Check:** S-06/S-28 style — heading vs owner sentence vs `INSERT`/`UPDATE`
in `zarabot/`. Reconcile with `KNOWN_UNOWNED_TABLES`.

**Do not:** give `broker.client` SQL. Do not implement the cache in this
amendment. Do not list `signals` as unowned.

**Modules:** spec §4/§5 and `scripts/ci/check_docs.py` (allowlist). Code
writers already match except `instruments`.
