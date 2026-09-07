# Proposal: #36 `/report` wiring belongs in `app.startup`'s contract

**Kind:** spec defect (failure class 2). Code already calls `set_report_builder(build_report)` in `zarabot/app/startup.py`. Spec and `tasks/32-app-startup.md` still do not name it. Delete the line and `/report` is `report unavailable` forever with `make check` green.

**Wrong / missing line:** `app.startup` ordered steps in `technical-spec.md` (step 8–9). `telegram.commands` documents `/report` handling but not who injects the builder. `set_report_builder` appears only in `interfaces.md`.

**Should say (in `app.startup`'s own section):** after halt restore (and reachability 8a), call `telegram.commands.set_report_builder(reporter.weekly.build)` before the ready alert. Clearing it is not required on shutdown (stateless commands).

**§3.2 cases that would have caught it:**

- After `start()`, a `/report` from the authorised chat returns a report body, not `report unavailable`.
- If `set_report_builder` is never called, the command replies `report unavailable` (negative control).

**Modules to re-run:** `32-app-startup` (add the test; keep the existing call). Optionally `29-telegram-commands` if the injection type is specified there.

**Do not:** invent a second builder; `interfaces.md` already records the signature.
