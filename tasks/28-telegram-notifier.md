# Task 28/42: Implement `zarabot/telegram/notifier.py`

## Product context

Pushes alerts. Never raises: Telegram being down must never delay a trading decision.

## Build order position

Module **28** of 42 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

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
- **This module owns rule 13 (v1.75), and the catch is narrow.** A send failure
  is `telegram.error.TelegramError` and only that; the retries — three — are for
  `telegram.error.NetworkError` (including `TimedOut`) and
  `telegram.error.RetryAfter`, the transport failures a second attempt can fix.
  `BadRequest`, `Forbidden` and `InvalidToken` are settings that will be equally
  wrong on the third attempt: logged once, not retried. **Every other exception
  propagates** out of `alert` to the caller, and thence to rule 21.
- That last clause is uncomfortable on purpose, because `alert` is called from
  inside other modules' `except` blocks. It has to be. This module carries every
  alert in the system, and an unqualified "never raises" means a rename here
  silences the whole channel while every module believes it has spoken — the one
  degraded state with no external symptom. The `except Exception` in
  `alert` today is that defect; `ruff`'s `BLE001` is the lint that would keep it
  from coming back, and it is not enabled yet.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

13. **Telegram send failure** → retry, then log. **Never propagates.** Telegram
    being down never delays or blocks a trading decision.

    **A send failure is `telegram.error.TelegramError`, and only that (v1.75)**
    Every failure the library reports — `NetworkError` and its `TimedOut`,
    `RetryAfter`, `BadRequest`, `Forbidden`, `InvalidToken` — is a subclass of
    it, so the class covers a chat misconfigured as completely as a network that
    is down, and both are conditions the caller can do nothing about mid-trade.
    **Retry is for the transport failures only** — `telegram.error.NetworkError`
    (including `TimedOut`) and `telegram.error.RetryAfter`. The rest are settings
    that will be just as wrong on the third attempt: they are logged once, not
    retried three times. **Any other exception propagates** to the caller and
    thence to rule 21.

    That last clause is the point of the amendment and it is deliberately
    uncomfortable, because `alert()` is called from inside other modules' `except`
    blocks: a defect in the notifier will now surface there rather than be
    absorbed. It has to. `telegram.notifier` carries every alert this system
    sends, and an unqualified "never propagates" means a rename inside it turns
    the whole alerting channel silent while every module believes it has spoken —
    the one degraded state with no external symptom at all (#107, failure class
    5). `ruff`'s `BLE001` is the mechanical companion to this rule and is **not
    enabled** in `pyproject.toml`; enabling it belongs with the code change that
    implements this narrowing, because `telegram/notifier.py`, `ops/backup.py`
    and `app/startup.py` all catch bare `Exception` today.

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
