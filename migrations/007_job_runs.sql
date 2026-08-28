-- When each periodic job last completed, per period.
--
-- This state lived in module globals, so every restart re-armed every job:
-- three restarts in a day produced three heartbeats, and a restart through the
-- Sunday 12:00-12:59 MSK hour lost that week's report entirely — no report, no
-- alert, no record (#27). Restarts are routine, not exceptional.
--
-- A second completion for the same period keeps the first ran_at: when the job
-- FIRST completed is the fact worth having, and it is what makes a late run
-- distinguishable from a repeated one.

CREATE TABLE job_runs (
    job         TEXT NOT NULL,
    period_key  TEXT NOT NULL,
    ran_at      TEXT NOT NULL,
    PRIMARY KEY (job, period_key)
);
