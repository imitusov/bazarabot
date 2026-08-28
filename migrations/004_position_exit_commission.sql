-- An EXTERNAL close has no closing order row, so the broker's fee on that sale
-- had nowhere to live and was simply lost, permanently overstating every
-- externally closed position's realised result (#11). Nullable with no default
-- and no backfill: the live database holds zero closed positions, and inventing
-- a historical value would be the same mistake in a new place.

ALTER TABLE positions ADD COLUMN exit_commission TEXT NULL;
