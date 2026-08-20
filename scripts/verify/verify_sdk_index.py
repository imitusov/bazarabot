#!/usr/bin/env python3
"""V10 - SDK availability and API surface. Requires no token.

The SDK is not on PyPI, so its availability is a build-time dependency on one
third party. This script also pins the API surface the design relies on, so a
future SDK version that renames or removes something load-bearing fails here
rather than in production.
"""

import hashlib
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _harness import Verifier  # noqa: E402

VENDOR = pathlib.Path(__file__).resolve().parents[2] / "vendor"
WHEEL = "t_tech_investments-1.49.1-py3-none-any.whl"
EXPECTED_SHA = "b18ea2da7ec4aec8fd9200ec70f3ec8e55df9ebf02173406ba55918fe2eb7eba"

v = Verifier("V10", "SDK availability and API surface")

wheel_path = VENDOR / WHEEL
if v.check("vendored wheel is present", wheel_path.exists(), str(wheel_path)):
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    v.check("wheel checksum matches", digest == EXPECTED_SHA, digest)

try:
    from t_tech.invest import AsyncClient, Client  # noqa: F401
    from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX  # noqa: F401
    from t_tech.invest.schemas import (  # noqa: F401
        CandleInterval,
        InstrumentIdType,
        OrderDirection,
        OrderIdType,
        OrderType,
    )
    imported = True
except Exception as exc:  # noqa: BLE001
    imported = False
    v.check("t_tech.invest imports", False, "{}: {}".format(type(exc).__name__, exc))

if imported:
    v.check("import root is t_tech (not tinkoff)", True)
    v.check("OrderIdType.ORDER_ID_TYPE_REQUEST exists",
            hasattr(OrderIdType, "ORDER_ID_TYPE_REQUEST"))
    v.check("sandbox endpoint constant exists", bool(INVEST_GRPC_API_SANDBOX),
            INVEST_GRPC_API_SANDBOX)

    from t_tech.invest.async_services import OrdersService

    post = inspect.signature(OrdersService.post_order).parameters
    state = inspect.signature(OrdersService.get_order_state).parameters

    v.check("post_order accepts order_id (idempotency key)", "order_id" in post)
    v.check("post_order accepts confirm_margin_trade", "confirm_margin_trade" in post)
    v.check("confirm_margin_trade defaults to False",
            post.get("confirm_margin_trade") is not None
            and post["confirm_margin_trade"].default is False)
    v.check("get_order_state accepts order_id_type", "order_id_type" in state)

    import t_tech.invest.constants as consts
    v.note("SDK version {}".format(getattr(consts, "APP_VERSION", "unknown")))

v.finish()
