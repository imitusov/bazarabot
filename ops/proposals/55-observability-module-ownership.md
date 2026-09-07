# Proposal: #55 remaining §7.1 events still have no owning module heading

**Kind:** spec defect (failure class 2). `startup_ok` is now in `app.startup` (v1.58). Other catalog events (`heartbeat`, `order_*`, `signal_*`, `moscow_time` on *every* record, etc.) still live only in §7.1, so regenerated tasks omit them.

**Should say:** for each event, one sentence in the emitting module's contract plus a §3.2 case. `logging_setup` must add `moscow_time` on every JSON record (UTC `timestamp` already exists). `event` remains a field producers set via `extra=` — do not add a second public logger API unless the spec names it.

**Modules to re-run after amendment (dependency order):** `04-logging_setup`, then each producer: `24-state-halt`, `27-broker-reconcile`, `26-execution-orders`, `33-app-loops`, `23-market-data`, `22-market-session`, `28-telegram-notifier`, `29-telegram-commands`, `31-ops-backup`, `30-reporter-weekly`. Host: `scripts/deploy/export_health.py` is not a generated task — specify tests under deploy tooling or a new registry entry.

**Do not:** implement all producers in one session; one module per chat.
