# Proposal: #101 S-11 — §5 adjustments catalogue must list every type

**Kind:** spec. §5 lists four JSON shapes: `CLOSED_EXTERNALLY`,
`EXIT_UNRESOLVED`, `ADOPTED`, `LOTS_ADJUSTED`. §4 also defines
`FOREIGN_HOLDING`, `STOP_MISSING`, `STOP_ORPHAN`, `STOP_MISPRICED`,
`STOP_ADOPTABLE`, `STOP_DUPLICATE` (`keep`/`cancel`). Pin `STOP_DUPLICATE`'s
wire shape: #35 was a dropped adjustment.

**Should say:** every type `broker.reconcile` can persist, with wire shape.
Keep `ADOPTED` in the catalogue. Rule 32 forbids adopting *unrecognised*
holdings only; `broker.reconcile` still calls `_adopt_holding` for the
order-recognised crash-recovery case (`technical-spec.md` keeps that
deliberately). `ADOPTED` is live: `app.startup` step 7 must recognise it
(observed, no remedy).

**Do not:** silently drop a type in `app.startup` step 7 (already an
incident: `STOP_DUPLICATE`, guarded at `app/startup.py` `_report_unhandled`).
`reporter.weekly` does not consume adjustments.

**Modules:** spec §5; `app.startup` if a type is added or renamed. Not
`reporter.weekly`.
