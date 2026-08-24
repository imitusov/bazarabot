# Task 21/40: Implement `zarabot/broker/client.py`

## Product context

The ONLY module that talks to the broker. Wraps t_tech.invest.AsyncClient and returns domain types. Run the verification suite before building this.

## Build order position

Module **21** of 40 in `dependency-order.md`. Everything before it is complete and tested — **do not modify any of it**.

## Already-implemented interfaces

**Read `interfaces.md` now.** It lists the exact, tested signatures of every completed module. Call those; never guess a signature and never reimplement something recorded there.

## Module contract

### `zarabot/broker/client.py`

The **only** module that calls the broker. Wraps `t_tech.invest.AsyncClient` and
returns domain types, never SDK types. The SDK's own services — `OrdersService`,
`MarketDataService`, `InstrumentsService`, `OperationsService`, `SandboxService`
— are reachable only from inside this module.

**Sandbox is selected by endpoint, never by a different method family.** The SDK
exposes a `SandboxService` with a parallel set of methods — `post_sandbox_order`,
`get_sandbox_order_state`, `cancel_sandbox_order` and so on. This module must
**not** use them for trading operations. `TRADING_MODE=sandbox` instead
constructs the client against `INVEST_GRPC_API_SANDBOX` and calls the ordinary
`orders.post_order` / `orders.get_order_state`, so sandbox and live exercise the
same code path and a sandbox result is evidence about live behaviour. Using the
`post_sandbox_*` family would mean testing code that never runs in production —
worse than not testing, because it produces false confidence. The
`SandboxService` methods are permitted only for account housekeeping with no live
equivalent (opening and funding a sandbox account), and only from verification
scripts, never from `zarabot/`.

**Margin trading is prohibited at the call site.** The SDK's `post_order` accepts
`confirm_margin_trade: bool = False`. This module must never pass `True`, under
any condition, for any order. The brief's no-leverage guarantee — which is what
bounds the maximum loss to the allocated capital, and therefore what the entire
security model rests on — is enforced here, at the one place an order can be
created. A code change setting this flag is a critical defect regardless of what
else it does.

**`async get_instrument(ticker: str) → Instrument`**
- Raises `InstrumentNotFound` when the ticker does not resolve, `BrokerUnavailable`
  on transport failure, `BrokerRateLimited` when throttled.

**`async get_candles(figi: str, interval: CandleInterval, since: datetime, until: datetime) → list[Candle]`**
- Returns candles ordered oldest-first with timezone-aware timestamps.
- Returns an empty list when the range contains no trading activity.
- Raises `ValueError` on naive datetimes.

**`PriceRejected`** — a quote arrived but is not usable. **Distinct from
`BrokerUnavailable`**, which means the broker could not be reached. Conflating
them makes a malformed field read as a network outage, so it counts toward the
consecutive-failure alert and is retried as though waiting would help.

**`async get_last_price(figi: str) → Decimal`**
- Validates the quote at the **single point prices enter the system**, and
  treats a bad price as missing data rather than as a signal. Raises
  `PriceRejected` when:
  - the price is **not strictly positive** — `Decimal(0)` currently flows
    straight through to `lifecycle.exits`, where `0 <= stop_price` is true for
    every position, so one degraded response liquidates the whole book at
    market;
  - the quote's timestamp is older than `price_max_age_seconds`;
  - the quote carries **no timestamp, or a naive one** — freshness that cannot
    be verified is not freshness. This is data from an outside system, so it is
    rejected as unusable rather than raising `ValueError` the way a naive
    datetime crossing an internal module boundary does;
  - the price differs from the last accepted price for that instrument by more
    than `price_max_move_pct`.
- Keeps the last accepted price per instrument, which is what makes the move
  check possible. This is the only state this module holds, and it is why the
  check cannot live in `lifecycle.exits`: that module is pure and has no memory
  of the previous tick.
- A rejected quote does not update the last accepted price. Accepting an
  implausible value as the new baseline would make the *next* implausible value
  look reasonable.

**`async get_portfolio() → PortfolioState`**
- Returns cash and holdings as reported by the broker. This is the authoritative
  view referred to throughout the brief.

**`async get_trading_schedule(days: int) → list[SessionInfo]`**
- Session open and close instants per day, timezone-aware, marking non-trading
  days.

**`async post_market_order(key: str, figi: str, side: Side, lots: int) → OrderRecord`**
- Submits a market order using `key` as the broker-side idempotency key.
- Raises `OrderRejected` carrying the broker's reason, `BrokerUnavailable`, or
  `BrokerRateLimited`.
- Must never be called before `db.orders.record_submitting` has persisted `key`.

**`async post_stop_loss(key: str, figi: str, lots: int, stop_price: Decimal) → StopOrderRecord`**
- Places a standing stop-loss with the exchange using
  `post_stop_order(stop_order_type=STOP_ORDER_TYPE_STOP_LOSS,
  expiration_type=STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
  exchange_order_type=EXCHANGE_ORDER_TYPE_MARKET)`, carrying its own idempotency
  key and `confirm_margin_trade=False`.
- Raises `StopOrderRejected` carrying the broker's reason, or `BrokerUnavailable`.
- Good-till-cancel is required: a day-expiring stop would silently stop
  protecting the position overnight, which is precisely when it is needed.

**`async cancel_stop_order(stop_order_id: str) → None`**
- Idempotent. An already-cancelled or already-executed stop order is not an
  error, because the executor calls this while racing the exchange.

**`async list_stop_orders() → list[StopOrderRecord]`**
- Every standing stop order on the account. Consumed by reconciliation.

**`async get_max_lots(figi: str) → int`**
- The maximum lots the broker will accept for a buy on this account. A pre-submit
  sanity check against `risk.sizing`, which models cash but not settlement or
  instrument-specific restrictions.

**`async get_operations(since: datetime, until: datetime) → list[OperationRecord]`**
- Executed operations including actual commission charged. Used for independent
  reconciliation of costs over a period — **not** as the per-order commission
  source: `OperationRecord` carries no order identifier, so attributing an
  operation to an order would mean matching on instrument, time and quantity,
  which is ambiguous exactly when two similar orders are close together.

**TLS requires the broker's own root certificate.** T-Bank's endpoint presents a
certificate chaining to the Russian Trusted Root CA, which gRPC's built-in trust
store does not contain. The SDK ships that root at
`t_tech/invest/certs/RussianTrustedRootCA.pem` but loads it **only** when the
environment variable `SSL_TBANK_VERIFY` is `true`; its default is `false`, so
every call otherwise dies in the TLS handshake with
`CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain`.

This is not a property of any particular network — it was reproduced from a
clean machine, and would fail identically on the VPS. `config` therefore exposes
`ssl_tbank_verify`, defaulting to **true**, and `app.startup` writes it into the
process environment immediately after `config.load()` and before any broker call.
`broker.client` must not read the variable itself: the SDK reads it from the
environment when a channel is created, so the only requirement is that it is set
before the first client is constructed.

**Commission comes back on the order itself.** Both `PostOrderResponse` and
`OrderState` carry `executed_commission`, keyed by our own idempotency key.
`post_market_order` and `get_order_state` therefore populate
`OrderRecord.commission` directly, with no matching and no ambiguity. Commission
is never estimated, and never inferred from an operations feed.

**`async get_order_state(key: str) → OrderRecord`**
- Retrieves an order **by the client idempotency key alone**, so a restarted
  process can determine what happened. Implemented as
  `get_order_state(account_id=…, order_id=<our key>, order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST)`.
  The enum member exists in the SDK (`OrderIdType.ORDER_ID_TYPE_REQUEST = 2`) and
  the parameter is present on the async method signature, so this is verified
  against the library, not only against documentation. Passing the exchange
  identifier here is a defect: after a crash the exchange identifier is precisely
  what was lost.
- The key passed to `post_order` as `order_id` and the key passed here are the
  same value — the row's primary key in the `orders` table.
- Raises `OrderNotFound` when the broker has no record, which proves the order
  was never accepted.
- **Documented fallback.** `PostOrder` is itself idempotent on the
  `(orderId, accountId)` pair: re-submitting with a key already used returns the
  status of the existing order rather than creating a second one. If
  `ORDER_ID_TYPE_REQUEST` proves unreliable in practice, recovery may re-call
  `post_market_order` with the original key, which is a safe read. Two
  independent recovery paths exist; the design does not rest on either alone.
- **Key retention caveat.** The broker states idempotency keys are retained for
  one year but explicitly declines to guarantee it, noting the mechanism may
  change. This design needs retention measured in minutes — from crash to
  restart — so the caveat is immaterial here, but it means keys must never be
  treated as a permanent audit identifier. The `orders` table is that record.

All functions in this module: must never log or include the token in any
exception; must convert every SDK exception into one of the typed exceptions
above; must never return a `float`.

## Relevant error handling rules

From `technical-spec.md` §8. Handle each exactly as written.

1. **Market data transport failure** → WARNING, exponential backoff, retry on the
   next cycle. After three consecutive failed cycles, alert **once**; keep the
   process alive and keep trying. Never exit.

2. **Broker rate limited** → WARNING, back off per the broker's hint. Alert once
   if sustained beyond five minutes. Never treat as fatal.

3. **Entry order rejected** → ERROR, record `broker_reason`, alert, open no
   position. **Never retried.**

4. **Exit order rejected or broker unreachable during an exit** → ERROR, alert
   **immediately**, retry on every following cycle until the position closes or
   the owner intervenes. The documented exception to rule 3.

5. **Order submission times out or the outcome is unknown** → leave the row
   `SUBMITTING`, resolve by querying with the idempotency key on the next cycle
   or at next startup. **Never resubmit.**

19. **Secret exposure** → no token is ever written to a log, an exception message,
    or a Telegram message. If the redaction filter detects a secret in an
    outgoing Telegram message, the message is **dropped**, and an alert reporting
    the incident without the secret is sent in its place.

28. **Any code path that would set `confirm_margin_trade=True`** → rejected in
    review, not at runtime. There is no runtime condition under which this is
    correct; it is listed here because the failure mode it would produce —
    losses exceeding allocated capital — is the one failure the brief promises
    cannot happen.

## Test cases

From `technical-spec.md` §3.2. Each becomes a real test, written FIRST.

- Each method returns the documented domain type given a scripted broker
  response (happy path per method).
- A transport error raises `BrokerUnavailable` (proves transport failures are
  typed, not leaked as SDK exceptions).
- A rate-limit response raises `BrokerRateLimited` carrying the retry hint
  (proves the caller can back off correctly).
- A zero-valued quote raises `PriceRejected`, not `BrokerUnavailable` and not
  `Decimal(0)` (proves the mass-liquidation path is closed at its source, and
  that bad data is distinguishable from an outage).
- A quote older than `price_max_age_seconds` raises `PriceRejected` (boundary:
  exactly at the threshold is accepted, one second beyond is not).
- A price more than `price_max_move_pct` from the last accepted price raises
  `PriceRejected`, and the last accepted price is **unchanged** afterwards
  (proves an implausible value cannot become the baseline that makes the next
  one look reasonable).
- An order rejection raises `OrderRejected` carrying the broker's reason string
  (proves the reason reaches the owner).
- No exception raised by this module contains the token in its message (proves
  the secret boundary).
- Prices returned are `Decimal` (proves no float leaks in from the SDK).
- Every order submission is asserted to pass `confirm_margin_trade=False`
  (proves the no-leverage guarantee is enforced at the only place it can be
  broken — this test is the executable form of the brief's loss bound).
- `get_order_state` is asserted to pass `ORDER_ID_TYPE_REQUEST` (proves recovery
  looks orders up by the key it still has, not the identifier it lost).

## Expected output

- `zarabot/broker/client.py` implementing the contract exactly
- `tests/test_broker_client.py` implementing every test case above
- All tests passing, coverage threshold met
- This module's public signatures appended to `interfaces.md`

## Agent instructions

1. Write `tests/test_broker_client.py` FIRST, from the test cases above. No implementation yet.
2. Run it. Confirm it **fails** — nothing is implemented.
3. Write `zarabot/broker/client.py` to satisfy the contract.
4. Run again. Iterate until all pass.
5. Match contract signatures EXACTLY, including `| None`.
6. Call interfaces as recorded; do not reimplement them.
7. Handle every error rule above as written.
8. Add no dependency outside `requirements-*.txt`.
9. Modify no module other than this one.
10. Append public signatures to `interfaces.md` once green.
11. If the contract is ambiguous, conflicts with `interfaces.md`, or a test
    cannot pass without violating it — **STOP and ask**. Do not guess.
