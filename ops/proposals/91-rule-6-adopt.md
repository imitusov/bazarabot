# Proposal: #91 S-01 — delete or rewrite rule 6 (never adopt)

**Kind:** spec. Brief wins. Built code matches rule 32 / `broker.reconcile`
(`FOREIGN_HOLDING`, never adopt). Rule 6 still says adopt.

**Wrong:** §8 rule 6: unknown broker order → adopt. Rule 32 and the brief:
refuse to start, never adopt. Adoption from average cost sold a position
already past take-profit.

**Should say:** rule 6 is gone, or is a one-line deferral to rule 32. §8
preamble: one rule per failure. Do not leave both.

**Do not:** implement adoption in `broker.reconcile`. Tests already expect
`FOREIGN_HOLDING`.

**Modules to re-run after amend:** regenerate `tasks/` for reconcile /
startup; no code change if the body already matches rule 32.
