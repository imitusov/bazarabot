# Proposal: rule 19 at `app.startup` and `config.Config.__repr__`

**Kind:** spec-conformant code defect outside `telegram.notifier`. O-10 / #64
review: `notifier._secrets` now includes `tinvest_account_id`. Two other sites
still omit it.

**`zarabot/app.startup.py` `configure(...)`** currently passes
`[cfg.tinvest_token, cfg.telegram_bot_token]`. The log redaction filter therefore
does not mask the account identifier. Add `cfg.tinvest_account_id`.

**`zarabot/config.py` `Config.__repr__`** replaces both tokens with `_REDACT` and
then interpolates `tinvest_account_id` in the clear. A traceback or debug log
of a `Config` is a live rule-19 violation. Redact that field the same way as the
tokens.

**Test contract:** `startup` `configure` secrets list includes the account id;
`repr(Config)` does not contain the configured account id.

**Modules to re-run:** `03-app-startup` (or whichever owns `configure` call) and
`01-config`. Not this notifier PR.

**Do not:** change those modules on the O-10 branch.
