# Proposal: #81 spec-vs-tests audit — do not “fix” tests to match code

**Kind:** test/spec. Issue is the ordered list. Approval was required before
remediation; several money items remain.

**Do first (money):** `execution.orders` margin-flag at this seam; float
guard; fill vs injected quote; recovered exit trigger from the **order row**.
`risk.gate` SELL → `ZERO_LOTS` reason, not merely `approved is False`.

**Do not:** change `db.orders` settle atomicity arguments to match a wrong
call; do not change `db.trading_days` until the holiday-after-the-fact case
is ruled (S-23/F-51).

**Sandbox last.** Worked example + live-identity are what make backtests
evidence.

One module per PR, test-first, after any spec conflict is amended.
