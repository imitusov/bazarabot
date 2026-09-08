# Proposal: #94 S-04 — delete rule 27; decide unbooked partial-exit P&L

**Kind:** spec. Rule 27: retry remainder until flat. §4 + rule 34: terminal
partial exit shrinks lots, leaves OPEN, sold slice **not booked**. Code
implements §4. Rule 27 is leftover.

**Should say:** delete rule 27. Keep rule 34 / §4 as the live rule unless
the brief wants the sold slice booked (then specify a store — not a blended
price; rule 33).

**Do not:** restore a slicing loop in `close_position` (#10). Do not invent
a P&L row in this amendment.

**Modules:** spec §8; `execution.orders` only if booking is specified later.
