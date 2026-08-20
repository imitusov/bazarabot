#!/usr/bin/env bash
# Runs the pre-development verification suite in dependency order.
# Stops at the first failure. Exits non-zero if any script fails.
#
# Partial success is failure: the point is to start development on verified
# ground, not on mostly-verified ground.

set -u
cd "$(dirname "$0")"

# Override with PYTHON=/path/to/python3.12 if the suite must run on a specific
# interpreter. The verify suite itself only needs the SDK, so 3.9+ is enough;
# the application needs 3.12.
PYTHON="${PYTHON:-python3}"

SCRIPTS=(
  verify_sdk_index.py        # V10 - no token
  verify_environment.py      # V9  - no token
  verify_token.py            # V1
  verify_instruments.py      # V3
  verify_candles.py          # V4
  verify_calendar.py         # V5
  verify_rate_limits.py      # V7
  verify_trading_rights.py   # V2  - sandbox, places a real order
  verify_order_state.py      # V6  - sandbox, places a real order
  verify_stop_orders.py      # V11 - sandbox, buys/protects/sells one lot
  verify_telegram.py         # V8  - needs you to reply /status
)

echo "zarabot pre-development verification"
echo "===================================="

for script in "${SCRIPTS[@]}"; do
  echo
  "$PYTHON" "$script"
  status=$?
  if [ $status -ne 0 ]; then
    echo
    echo "SUITE FAILED at ${script}. Fix this before running anything after it."
    exit 1
  fi
done

echo
echo "SUITE PASSED - all checks green. Record the measured values in"
echo "technical-spec.md §9 and pin the lockfile before writing code."
