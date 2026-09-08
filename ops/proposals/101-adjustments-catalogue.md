# Proposal: #101 S-11 — §5 adjustments catalogue must list every type

**Kind:** spec. §5 lists four JSON shapes: `CLOSED_EXTERNALLY`,
`EXIT_UNRESOLVED`, `ADOPTED`, `LOTS_ADJUSTED`. §4 also defines
`FOREIGN_HOLDING`, `STOP_MISSING`, `STOP_ORPHAN`, `STOP_MISPRICED`,
`STOP_ADOPTABLE`, `STOP_DUPLICATE` (`keep`/`cancel`). `ADOPTED` in §5
contradicts rule 32 / S-01.

**Should say:** every type `reporter` / reconcile can persist, with wire
shape. Pin `STOP_DUPLICATE`'s `keep`/`cancel`. Drop `ADOPTED` from the
catalogue if adoption is forbidden (S-01), or keep it only as historical
rows.

**Do not:** silently drop a type in `reporter.weekly` (already an incident).

**Modules:** spec §5; `broker.reconcile` / `reporter.weekly` only if a type
is added or renamed.
