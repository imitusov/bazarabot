# Proposal: #52 distinguish a history write failure from a programming error

**Kind:** spec defect (failure class 5). `zarabot/market/session.py` §3.2 currently says:

> A write failure is logged at ERROR and does not propagate: an unavailable history degrades age counting, which `covers` then reports, and must not stop the bot trading.

That sentence is true for `aiosqlite.Error`. It is false for `AttributeError`. Catching `Exception` made a rename look like an unmeasurable calendar.

**Should say (one sentence next to the write-failure line):** a history **write failure** is `aiosqlite.Error` from `record_many` or the history reload — logged at ERROR, does not propagate. Any other exception from that path **propagates**. `refresh` still does not raise for an unavailable broker schedule (rule 10).

**Test contract:** `aiosqlite.Error` leaves the schedule cached and `is_open` true. `AttributeError` from `record_many` propagates.

**Modules to re-run:** `22-market-session` only. Implementation already exists on the F-52 branch; do not merge that PR until this amendment lands, and do not record the narrowed catch in `interfaces.md` before the spec says it.

**Do not:** catch `Exception` again to satisfy the old wording.
