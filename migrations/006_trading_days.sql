-- What the broker said about each calendar day, recorded when it said it.
--
-- The broker serves no trading schedule for any date before today: every
-- from_ earlier than today's midnight is rejected with INVALID_ARGUMENT /
-- 30003, measured across seven ranges against the live account (§2.1). So the
-- only way to know whether last Tuesday was a trading day is to have been told
-- at the time and to have written it down (#45).
--
-- Unlike every other table here this one is NOT append-only: a day is
-- overwritten by a newer observation, because a holiday can be announced after
-- the fact and the broker's most recent answer is the one to keep. It records
-- what is true about a date, not what happened.

CREATE TABLE trading_days (
    trade_date      TEXT PRIMARY KEY,
    is_trading_day  INTEGER NOT NULL CHECK (is_trading_day IN (0, 1)),
    session_start   TEXT NULL,
    session_end     TEXT NULL,
    observed_at     TEXT NOT NULL
);
