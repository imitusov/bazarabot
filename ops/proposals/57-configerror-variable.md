# Proposal: `ConfigError` must carry `variable`, not a parsed message

**Kind:** failure class 13. `app.startup` (PR #130) currently does:

```python
def _config_variable(exc: ConfigError) -> str:
    return str(exc).split()[0]
```

Every raise in `config.py` happens to begin with the variable name today. Nothing in the `config` contract says that. The next message that starts with `"Missing"` puts `"Missing"` in `config_invalid.variable`. `TAKE_PROFIT_PCT must be greater than STOP_LOSS_PCT` names two variables and reports one.

This is the same shape as v1.62 deriving a table name from SQLite error strings, replaced by v1.64 with a caller-held value.

**Should say:** `ConfigError` has a `variable: str` attribute set at every raise site from the local `name`. `app.startup` reads `exc.variable`. It does not split the message.

`Config.__repr__` still interpolates `tinvest_account_id` in the clear (rule 19). Redact it the same way as the tokens. That is the third site from the O-10 review; `notifier` and `app.startup.configure` are already covered.

**Test contract:** `ConfigError.variable` equals the env name for a missing required var. `repr(Config)` does not contain the configured account id. `config_invalid` uses the attribute, not `str(exc).split()`.

**Modules to re-run:** `01-config`, then `32-app-startup` to drop `_config_variable`.

**Do not:** keep splitting the message in `app.startup` after `config` grows the attribute.
