-- Append-only trail of every position mutation. Owned by db.positions;
-- this file belongs to db.migrations because 001 is already applied.

CREATE TABLE position_events (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES positions (id),
    occurred_at TEXT NOT NULL,
    event TEXT NOT NULL CHECK (
        event IN (
            'OPENED',
            'STOP_PROTECTION_CHANGED',
            'LOTS_ADJUSTED',
            'CLOSED',
            'REALISED_RECOMPUTED',
            'ADOPTED'
        )
    ),
    detail TEXT NOT NULL
);
