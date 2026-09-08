# Proposal: #107 S-17 — rules 11/12/13/18 name exception classes

**Kind:** spec. §4 already narrowed DB writes to `aiosqlite.Error` (v1.59)
and the same for `market.data`. §8 still reads as `except Exception`.

**Should say:** rule 11/12: `aiosqlite.Error` only. Rule 13: the Telegram
HTTP/timeout types `telegram.notifier` already retries — not `Exception`.
Rule 18: backup copy errors, not `Exception`. BLE001 stays.

**Do not:** widen catches in `telegram.notifier` or `ops.backup`.

**Modules:** spec §8. Code already narrowed where v1.59 landed.
