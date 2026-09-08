# Proposal: #67 `weekly_report_sent` names a delivery this module cannot observe

**Kind:** spec naming defect. Implementation on PR #129 matches the contract as written.

§4 `reporter.weekly.send`:

> On a successful send, emit `weekly_report_sent` (INFO) with `period_start` and `period_end`. A failed send emits nothing of this name.

§8 rule 13: Telegram send failure retries, logs, **never propagates**. `telegram.notifier.alert` returns `None` after three failed attempts. So `await alert(text)` returning is not evidence of delivery, and `weekly_report_sent` fires on a total Telegram outage.

The existing test stubs `alert` to raise. Production `alert` never raises, so that branch is not the production failure path.

**Should say** one of:

1. Rename the event to what this module actually witnesses, e.g. `weekly_report_built` — the report was composed and handed to the notifier. Rule 13 stays intact. An operator who needs delivery still has `telegram_send_failed`.
2. Or: `alert` returns whether the last attempt succeeded, and `send` emits `weekly_report_sent` only then. That undoes rule 13's "callers never care" for one log line and is a change to `telegram.notifier`.

Prefer (1). Do not have `reporter.weekly` invent a success flag.

**Test contract:** after `alert` returns, the event still fires even if the notifier swallowed a send failure. Delivery is `telegram_send_failed` absent, not `weekly_report_sent` present.

**Modules to re-run:** `30-reporter-weekly` after the rename, or `28-telegram-notifier` if option 2.

**Do not:** make `alert` raise so the current test's fixture matches production.
