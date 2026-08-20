-- Add the exit-reason column so a recovered EXIT fill can be attributed.
-- CHECK matches the spec: STOP_LOSS / TAKE_PROFIT / MAX_AGE, or NULL on ENTRY.

ALTER TABLE orders ADD COLUMN exit_trigger TEXT
    CHECK (
        exit_trigger IS NULL
        OR exit_trigger IN ('STOP_LOSS', 'TAKE_PROFIT', 'MAX_AGE')
    );
