# Proposal: #166 S-34 — `ConfigError.variable` belongs in the spec, not only `interfaces.md`

**Kind:** spec. Cursor never edits `technical-spec.md`. `interfaces.md` already
records the constructor; the spec does not. This is not a `config` re-run.

**Wrong:** `interfaces.md` says `ConfigError(message: str, *, variable: str)` —
`variable` is the env var name set at the raise site, never parsed from the
message. `technical-spec.md` has no such attribute. §3.2 (missing `TINVEST_TOKEN`)
and §4's `ConfigError` bullet only require that **the message** names the
offender. A re-run of `01-config` from the spec alone would drop the keyword
and take #135's fix with it. `#164`'s two tests came from the issue, not the
contract. `check_docs.py` check 3 compares §4 *function* signatures; the
constructor is invisible to it (same shape as #161).

**Should say**, in §4 `zarabot/config.py`:

> `ConfigError(message: str, *, variable: str)` — `variable` is the environment variable name, set at the raise site from the name being validated. It is never derived by parsing the message. Where a check compares two variables, `variable` is the one being validated, and the message names both.

**§3.2** gains two cases:

- A cross-field failure whose message names two variables sets `variable` to the field being validated (`TAKE_PROFIT_PCT` for the take-profit-versus-stop-loss check), and the message still names both.
- A raise site that omits `variable` is a `TypeError`, not a wrong field — the required keyword is the guarantee, and a test asserting the attribute alone would pass against a hardcoded literal.

The second case is the one worth having. #164 has a test for the first; the
weaker of its two tests would stay green against `variable="TINVEST_TOKEN"`
hardcoded inside `_require`, because that message also begins with that word.

**Modules to re-run:** `01-config`, tests only — for the `TypeError` case below.
The implementation already does this; the spec is catching up to it, but the
§3.2 case this proposal calls "the one worth having" has no test today
(`grep -c TypeError tests/test_config.py` is 0). An amendment that adds a §3.2
case and dispatches nobody to write it leaves an obligation with no code line,
which is the thing §3.2 cases exist to prevent.

Nothing mechanical will catch a `config` re-run that drops `variable=`. Check 3
compares §4 against `interfaces.md` — two documents; it never reads Python, and
its `SIG` regex requires an arrow return type a constructor bullet does not
have. That `TypeError` test is the only guard, so it must be dispatched here.

**Placement:** make the signature a `**\`ConfigError(message: str, *, variable:
str)\`**` sub-heading under §4 `zarabot/config.py`, not another prose bullet
under `load()`. Not because check 3 would then see it — it would not — but so a
re-run reads it as a signature to match rather than commentary.

**Also amend `strategies.registry`.** `registry.py:31` raises
`ConfigError(..., variable="ENABLED_STRATEGIES")`, while §4's registry sentence
says only "raising `ConfigError` on an unknown name". A re-run of that task from
the spec alone writes the two-argument call and hits `TypeError`. Either add the
clause there or state that §4's `config` bullet governs every raise site
project-wide.

**Do not:** implement `app.startup` here. #135 is still open — `app.startup`
reads `exc.variable` and `_config_variable` goes. That leftover is a later
session. Do not treat this as a `config` module re-run.

**Related:** #135, #161, #138 (gates whose stated scope is wider than their
real one). Earlier proposal `57-configerror-variable.md` asked for the
attribute; this file is the spec text that still has not landed.
