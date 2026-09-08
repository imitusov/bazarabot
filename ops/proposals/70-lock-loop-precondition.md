# Proposal: concurrent event loops must not silently drop submission exclusion (F-50 leftover)

**Kind:** spec gap after #140. The #50 defect (second sequential `asyncio.run` after contention) is fixed. This is a new shape the fix introduced.

#140 rebuilds `_global_lock`, `_registry_lock`, and `_ticker_locks` when `get_running_loop()` is not `_lock_loop`. Sequential `asyncio.run` (production, `sandbox.backtest.run`) is then safe.

If two loops are ever alive **at the same time** in one process — a thread with its own loop, or `asyncio.run` from a sync callback while the main loop still lives — each rebuild hands its caller freshly unlocked objects. Mutual exclusion is gone **with no error**. Pre-#140 raised `RuntimeError` loudly. The safety property has become "at most one loop at a time" and nothing enforces it. `interfaces.md` documents the rebuild, not the precondition.

No such caller exists today. A duplicate entry would be the money consequence.

**Should say** in §4 `execution.orders`:

> Submission locks belong to one event loop. Replacing them while a lock is held is a contract violation: raise. Two loops must not submit concurrently in one process.

**Cheap guard (after the amendment):** in `_loop_locks()`, if `_lock_loop` is being replaced and `_global_lock` is not `None` and `_global_lock.locked()`, raise. Sequential `asyncio.run` still succeeds because the previous loop is finished and no lock is held.

**Test contract:** two sequential contended `asyncio.run` calls still succeed (existing test). A nested/overlapping loop that rebuilds while the global lock is held raises. Do not add a thread-based production path until this is specified.

**Modules to re-run:** `26-execution-orders` only.

**Do not:** implement the raise in code first — it is a new failure mode and needs the sentence. Do not "fix" it by sharing one lock across loops (that reintroduces #50).
