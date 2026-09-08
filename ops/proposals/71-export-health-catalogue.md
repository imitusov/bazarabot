# Proposal: derive export_health's event catalogue from §7.1

**Kind:** spec/tooling. `KNOWN_EVENTS` in `scripts/deploy/export_health.py` is a
hand-copied 38-name frozenset labelled "Spec §7.1 catalogue". Nothing parses
the table. The next amendment that adds an event makes a healthy bot report
`unknown` and exit non-zero — a false alarm on the only production evidence path
in git.

**Should say:** the host export's allowed names are the §7.1 table, generated
or parsed once (including `session_open / session_closed` as two events).
`check_docs.py` or a unit test fails when the frozenset and the table disagree.

**Do not:** keep editing the frozenset by hand as the catalogue grows.

**Modules:** host tooling only (`scripts/deploy/export_health.py`, possibly
`scripts/ci/check_docs.py`).
