# Task 4/40: Implement `zarabot/logging_setup.py`

## Product context

Structured logging with secret redaction. A leaked token in a log file is equivalent to a leaked brokerage password.

## Build order position

Module **4** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/logging_setup.py`

Configures structured logging and enforces secret redaction.

**`configure(level: str, secrets: list[str]) → None`**
- Installs a JSON formatter writing to stdout and a filter that replaces every
  occurrence of every value in `secrets` with a fixed mask.
- Redaction applies to the message, to structured fields, and to formatted
  exception text, recursing into nested dictionaries and sequences to a depth of
  10; deeper structures are replaced wholesale rather than passed through
  unredacted.
- Must never write to a file, and never to stderr.
- **Emits the base record schema of §7.1 on every record (v1.60):**
  `timestamp` (UTC, ISO 8601), `moscow_time` (the same instant as
  `Europe/Moscow`, via `clock.to_moscow`), `level`, `logger` and `message`.
  Moscow time is derived here rather than by producers so that no caller can
  omit it and no caller needs a second timestamp argument.
- **`event` is producer-set and passes through unaltered (v1.60).** A record
  logged with `extra={"event": ...}` carries that field; a record logged without
  one carries no `event` key, and this module never invents a default. §7.1 said
  "all records carry … `event`", which is unsatisfiable for the records that
  third-party libraries emit through the same handler — `httpx`, `telegram.ext`
  and the broker SDK all log here and know nothing of the catalogue. The
  obligation to name an event belongs to each producing module's own contract,
  not to this one, and the absence of `event` is what marks a line as library
  noise rather than a domain event.
- **This module emits no domain event of its own.** It formats and redacts; it
  never originates a catalogued event. A `startup_ok` from here would be a
  fiction.
- The redaction filter applies to `event` as it does to every other structured
  field.
- Timestamps come from the `LogRecord`'s own `created` instant, not from
  `clock.now()` — this module is below `clock` in the dependency order and must
  not reach forward to it for the current time. `clock.to_moscow` is a pure
  conversion and is the one thing it does use.
- Called by `app.startup` immediately after `config.load()` and before any other
  module logs anything.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

19. **Secret exposure** → no token is ever written to a log, an exception message,
    or a Telegram message. If the redaction filter detects a secret in an
    outgoing Telegram message, the message is **dropped**, and an alert reporting
    the incident without the secret is sent in its place.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A log record whose message contains the token value emits the token replaced by
  a fixed mask (proves redaction on the message).
- A log record carrying the token in a structured field is redacted (proves
  redaction is not message-only).
- An exception whose string representation contains the token is redacted when
  logged with a traceback (proves redaction survives exception formatting).
- A record containing no secret passes through byte-identical apart from the
  base schema fields (proves redaction does not corrupt ordinary logs).
- Every record carries `timestamp`, `moscow_time`, `level`, `logger` and
  `message`, and `moscow_time` is the same instant as `timestamp` converted to
  `Europe/Moscow` (v1.60; proves the base schema of §7.1 is emitted here and
  cannot be forgotten by a producer).
- A record logged with `extra={"event": "heartbeat"}` carries `event`
  unaltered, and a record logged without one carries no `event` key (v1.60;
  proves the field is producer-set and that this module invents nothing).
- A token inside the `event` field is redacted (proves redaction reaches the
  field the whole catalogue is keyed on).

## Expected output

- `zarabot/logging_setup.py` implementing the contract exactly
- `tests/test_logging_setup.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_logging_setup.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/logging_setup.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
