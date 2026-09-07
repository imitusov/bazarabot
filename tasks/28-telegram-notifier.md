# Task 28/40: Implement `zarabot/telegram/notifier.py`

## Product context

Pushes alerts. Never raises: Telegram being down must never delay a trading decision.

## Build order position

Module **28** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/telegram/notifier.py`

**`async alert(text: str, urgent: bool = False) → None`**
- Sends to the configured chat. Retries on failure, then logs and returns.
- **After the last failed attempt, emit `telegram_send_failed` (WARNING) with
  `attempt` and `error` (v1.61).** Never raises.
- **When a body contains a configured secret, drop it, emit `secret_redacted`
  (ERROR) with `sink` equal to `telegram` — never the secret, never its length —
  and send a substitute incident alert without the secret (v1.61, rule 19).**
- Never includes a token or account identifier in a message.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

13. **Telegram send failure** → retry, then log. **Never propagates.** Telegram
    being down never delays or blocks a trading decision.

19. **Secret exposure** → no token **and no account identifier** is ever written
    to a log, an exception message, or a Telegram message. If the redaction
    filter detects a secret in an outgoing Telegram message, the message is
    **dropped**, `secret_redacted` is emitted, and an alert reporting the
    incident without the secret is sent in its place.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- A failed send is retried and, if still failing, emits `telegram_send_failed`
  without raising (v1.61; proves Telegram outages never reach trading logic).
- A body containing a token is dropped, emits `secret_redacted` with `sink`
  `telegram`, and does not contain the token (v1.61).
- No alert body contains either token (proves the secret boundary at the last
  point of egress).

## Expected output

- `zarabot/telegram/notifier.py` implementing the contract exactly
- `tests/test_telegram_notifier.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_telegram_notifier.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/telegram/notifier.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
