# Task 35/40: Implement `zarabot/__main__.py`

## Product context

Process entry point so that `python -m zarabot` works. No logic of its own.

## Build order position

Module **35** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/__main__.py`

The process entry point, so that `python -m zarabot` is the start command.

**`main() → int`**
- Installs signal handlers for `SIGTERM` and `SIGINT`, runs `app.startup.start()`,
  then `app.loops.run()`, and routes either signal to `app.shutdown.shutdown()`.
- Returns 0 on a clean shutdown, non-zero on `StartupError`.
- Contains no application logic of its own. Anything worth testing belongs in
  `app.*`; this module exists only to be the thing Python executes.
- On `StartupError` it sleeps 30 seconds before returning, so the container
  restart policy cannot produce an alert loop (rule 15).

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

15. **Configuration missing or invalid at startup** → refuse to start, alert if
    Telegram credentials are among the valid ones, sleep 30 seconds, exit
    non-zero. The sleep exists so the container restart policy cannot produce an
    alert loop.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

No dedicated test block in §3.2. Derive cases from the contract above: happy path, every early return, every boundary, and every documented exception.

## Expected output

- `zarabot/__main__.py` implementing the contract exactly
- `tests/test___main__.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test___main__.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/__main__.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
