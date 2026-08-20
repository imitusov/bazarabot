# Pure decision logic — 95% coverage

No I/O, no database, no broker, no clock except a `now` argument. These modules
are imported unchanged by `sandbox.backtest`; an I/O dependency here invalidates
every backtest.

## Determinism of reported reasons

Where several conditions apply at once, the reported one is fixed by the
priority order in the spec — never by evaluation order. `risk.gate` rejection
reasons and `lifecycle.exits` trigger precedence are both specified exactly.
Statistics built on a non-deterministic reason are noise.

## Stop-loss ownership

`lifecycle.exits` returns `STOP_LOSS` **only** when
`position.stop_protection == LOCAL`. When the exchange holds the stop, this
module must not return it — both owners acting sells the position twice.
Ownership is read from the position, never inferred.

## Coverage

**95%.** Every branch, every boundary, inclusive and exclusive sides tested.
