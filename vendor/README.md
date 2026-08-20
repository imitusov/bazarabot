# Vendored dependencies

## `t_tech_investments-1.49.1-py3-none-any.whl`

The official T-Invest Python SDK. **It is not published on PyPI** — both
`tinkoff-investments` and `t-tech-investments` return HTTP 404 from pypi.org.
It is published only to a T-Bank-hosted package index:

    https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

The wheel is vendored here so that building the image never depends on that
index being reachable. If it is down on the day you need to ship a fix during a
trading session, the build must still work.

Verify before use:

    shasum -a 256 -c SHA256SUMS

### Refreshing to a new version

Refresh deliberately, as its own change, never as a side effect of another one:

    curl -s "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple/t-tech-investments/" \
      | grep -o 'href="[^"]*"'

Download the wheel, replace the file, regenerate SHA256SUMS, then re-run
`scripts/verify/verify_sdk_index.py` — it checks that the API surface this
project depends on still exists.

### API surface this project depends on

- `from t_tech.invest import AsyncClient` — note the import root is `t_tech`,
  **not** `tinkoff`. The SDK's own docstrings still say `tinkoff.invest`; they
  are stale.
- `OrderIdType.ORDER_ID_TYPE_REQUEST` — order lookup by client idempotency key.
- `orders.post_order(..., order_id=, confirm_margin_trade=)`
- `orders.get_order_state(..., order_id_type=)`
- `constants.INVEST_GRPC_API_SANDBOX` — the sandbox endpoint.
