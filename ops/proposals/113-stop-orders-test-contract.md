# Proposal: #113 S-23 — `db.stop_orders` needs a §3.2 heading

**Kind:** spec. Full §4 including terminal `OrderStateError` and “never more
than one active stop”. No `**db.stop_orders**` in §3.2. Tests exist in
`tests/test_db_stop_orders.py`. `make_tasks` has `tkey=None` so the task
says invent cases. Closest cases sit under execution/reconcile.

**Should say:** a §3.2 block: record/activate/settle, refuse terminal→live,
`list_active` empty not None, more-than-one-active is the stated error.

**Do not:** delete `tests/test_db_stop_orders.py`.

**Modules:** spec §3.2; then `make_tasks` key like S-24. `08-db-stop_orders`.
