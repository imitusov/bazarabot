# Proposal: derive export_health's event catalogue from §7.1, and count
# expected heartbeats on trading days

**Kind:** spec/tooling. Two gaps in the same host export.

## Catalogue

`KNOWN_EVENTS` in `scripts/deploy/export_health.py` is a hand-copied 38-name
frozenset labelled "Spec §7.1 catalogue". Nothing parses the table. The next
amendment that adds an event makes a healthy bot report `unknown` and exit
non-zero — a false alarm on the only production evidence path in git.

**Should say:** the host export's allowed names are the §7.1 table, generated
or parsed once (including `session_open / session_closed` as two events).
`check_docs.py` or a unit test fails when the frozenset and the table disagree.

**Do not:** keep editing the frozenset by hand as the catalogue grows.

## Heartbeat denominator

§7.1: expected heartbeats are **trading days** over observed heartbeats.
**Days the exchange was closed are excluded from the denominator** rather than
counted as downtime.

Today: `health["expected_heartbeats"] = args.days` (calendar days, weekends
in). The fail threshold is `heartbeat < 1`, so one heartbeat in seven days
is ~14% uptime and still exits 0.

**Should say:** denominator = trading days in the window (from
`db.trading_days` or the session cache). Fail when observed/expected is below
a named ratio, not when the count is merely zero. Do not invent that ratio
in `export_health.py` first.

**Do not:** treat calendar `--days` as trading days.

**Modules:** host tooling only (`scripts/deploy/export_health.py`, possibly
`scripts/ci/check_docs.py`). The unit `zarabot-health.service` must fail when
the git push fails (`|| status=1`); that is code, not this catalogue gate.
