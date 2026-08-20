-- Initial schema. Money is TEXT (decimal strings); timestamps are TEXT ISO-8601 UTC.

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE orders (
    key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    figi TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    intent TEXT NOT NULL CHECK (intent IN ('ENTRY', 'EXIT')),
    lots INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'SUBMITTING',
            'SUBMITTED',
            'FILLED',
            'REJECTED',
            'CANCELLED',
            'UNKNOWN'
        )
    ),
    filled_lots INTEGER,
    filled_price TEXT,
    commission TEXT,
    broker_reason TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    figi TEXT NOT NULL,
    strategy TEXT NOT NULL,
    lots INTEGER NOT NULL CHECK (lots > 0),
    lot_size INTEGER NOT NULL,
    entry_price TEXT NOT NULL,
    entry_at TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    target_price TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    adopted INTEGER NOT NULL DEFAULT 0,
    open_order_key TEXT NOT NULL REFERENCES orders (key),
    close_order_key TEXT REFERENCES orders (key),
    exit_trigger TEXT CHECK (
        exit_trigger IN ('STOP_LOSS', 'TAKE_PROFIT', 'MAX_AGE', 'EXTERNAL')
    ),
    exit_price TEXT,
    exit_at TEXT,
    realised_pnl TEXT,
    stop_protection TEXT NOT NULL CHECK (stop_protection IN ('EXCHANGE', 'LOCAL')),
    stop_order_key TEXT,
    CHECK (
        (
            status = 'OPEN'
            AND exit_trigger IS NULL
            AND exit_price IS NULL
            AND exit_at IS NULL
            AND realised_pnl IS NULL
        )
        OR (
            status = 'CLOSED'
            AND exit_trigger IS NOT NULL
            AND exit_price IS NOT NULL
            AND exit_at IS NOT NULL
            AND realised_pnl IS NOT NULL
        )
    ),
    CHECK (
        (
            stop_protection = 'EXCHANGE'
            AND stop_order_key IS NOT NULL
        )
        OR (
            stop_protection = 'LOCAL'
            AND stop_order_key IS NULL
        )
    )
);

CREATE UNIQUE INDEX idx_positions_one_open
ON positions (ticker)
WHERE status = 'OPEN';

CREATE TABLE stop_orders (
    key TEXT PRIMARY KEY,
    stop_order_id TEXT,
    position_id INTEGER NOT NULL REFERENCES positions (id),
    ticker TEXT NOT NULL,
    lots INTEGER NOT NULL,
    stop_price TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'PLACING',
            'ACTIVE',
            'CANCELLED',
            'EXECUTED',
            'ORPHANED',
            'FAILED'
        )
    ),
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX idx_stop_orders_one_live
ON stop_orders (position_id)
WHERE status IN ('ACTIVE', 'PLACING');

CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    rejection_reason TEXT,
    lots INTEGER,
    order_key TEXT REFERENCES orders (key),
    CHECK (
        (
            decision = 'REJECTED'
            AND rejection_reason IS NOT NULL
        )
        OR (
            decision = 'APPROVED'
            AND lots IS NOT NULL
        )
    )
);

CREATE TABLE cooldowns (
    ticker TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);

CREATE TABLE daily_snapshots (
    trade_date TEXT PRIMARY KEY,
    opening_equity TEXT NOT NULL,
    closing_equity TEXT,
    cash TEXT NOT NULL,
    realised_pnl TEXT NOT NULL,
    unrealised_pnl TEXT NOT NULL,
    open_positions INTEGER NOT NULL,
    orders_placed INTEGER NOT NULL,
    benchmark_value TEXT
);

CREATE TABLE halt_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    halted INTEGER NOT NULL CHECK (halted IN (0, 1)),
    reason TEXT CHECK (
        reason IN (
            'DAILY_LOSS_LIMIT',
            'MANUAL',
            'RECONCILIATION_MISMATCH'
        )
    ),
    detail TEXT,
    halted_at TEXT,
    resumed_at TEXT,
    resumed_by TEXT
);

INSERT INTO halt_state (id, halted) VALUES (1, 0);

CREATE TABLE instruments (
    figi TEXT PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    lot INTEGER NOT NULL,
    min_price_increment TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'RUB'),
    trading_status TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);

CREATE TABLE reconciliations (
    id INTEGER PRIMARY KEY,
    ran_at TEXT NOT NULL,
    adjustments TEXT NOT NULL
);
