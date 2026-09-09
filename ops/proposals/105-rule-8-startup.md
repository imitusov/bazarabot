# Proposal: #105 S-15 — rule 8 startup half needs an owner or a deletion

**Kind:** spec. Rule 8 is **lot metadata**: unavailable mid-session → skip
+ WARNING; unavailable at startup → `StartupError`; must not trade an
instrument whose lot size it cannot confirm. **8a is a different
obligation** (affordability of `instrument.lot × price` vs
`position_budget`). Do not treat 8a's “never raises” as the same sentence
as rule 8's `StartupError`.

Code: `app/startup.py:297` `except Exception:` swallows `get_instrument`
(and price) into `unknown` and continues. No test pins rule 8's startup
half. Name **`app.startup`**.

**Should say — option 2 is the live-account choice.** Spec 8a
(`:3405-3409`) already argues against refusing to start:

> This step never raises `StartupError`, and never prevents startup. An
> unaffordable budget stops *new entries only*. **Refusing to start would
> additionally abandon every open position** — no exit evaluation, no
> stop management, no `MAX_AGE` — converting a benign no-op into an
> unmanaged holding with real money in it. The bot must keep running to
> protect what it holds.

That quote is 8a (budget), but the **money path is the same** if rule 8's
startup half refuses: open positions lose the process. Option 1 is that
branch. Do not present 1 and 2 as equal.

1. **Money-losing if chosen without another process owning exits.** Rule 8
   startup half owned by `app.startup`: any watchlist ticker whose lot
   size cannot be confirmed → `StartupError`. Then 8a's “never raises”
   stays scoped to **affordability**, not lot metadata — or the two
   sentences collide.
2. **Weight this.** Rule 8 is mid-session only. Startup may start with a
   partial watchlist (current code). Delete the `StartupError` sentence.
   Missing lots stay excluded from 8a's judgement (`unknown`), bot keeps
   protecting what it holds.

**Do not:** implement `StartupError` on missing lots while the spec still
forbids refusing to start; do not conflate 8a affordability with rule 8
lot metadata.

**Modules:** spec first. Then `32-app-startup` / `app.startup` only if (1).
The `:297` catch is this module either way.
