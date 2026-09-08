# Proposal: #105 S-15 — rule 8 startup half needs an owner or a deletion

**Kind:** spec. Rule 8: metadata unavailable at startup → `StartupError`.
`app.startup` 8a: never raises, excludes the ticker from the affordability
judgement. Code swallows `get_instrument` failure. No test pins the startup
half.

**Should say** one of:

1. Rule 8's startup half is owned by `app.startup` (or a named step): any
   watchlist ticker whose lot size cannot be confirmed → `StartupError`.
   Delete 8a's “never raises”. Or
2. Rule 8 is mid-session only. Startup may start with a partial watchlist
   (current code). Then delete the `StartupError` sentence.

**Do not:** implement `StartupError` on missing lots while 8a forbids it.

**Modules:** spec first. Then `32-app-startup` only if (1).
