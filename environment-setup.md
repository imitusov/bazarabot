# Environment Setup — Zarabot

**Version:** 1.0
**Derived from:** `technical-spec.md` v1.5
**Versioning:** new version when a prerequisite, variable, or setup step changes.

Follow these steps in order. **Do not begin implementation until all five
verification checks in §7 pass.** A subtle environment problem surfaces three
modules into the build as a confusing failure; the checks catch each class of
problem up front, in isolation.

This project trades real money on a live brokerage account. §3 and §4 involve
credentials — read them before running anything.

---

## 1. Prerequisites

| Tool | Minimum | Why |
|---|---|---|
| Python | **3.12** | numpy 2.5 and pandas 3.0 both require it |
| git | any recent | |
| Docker + Compose plugin | any recent | Deployment target; not needed for local development |

```bash
python3.12 --version && git --version
```

macOS: `brew install python@3.12`. Your system Python is likely 3.9, which runs
the verification scripts but not the application.

## 2. Clone and install

```bash
python3.12 -m venv .venv && .venv/bin/pip install --upgrade pip
```

**Verified working on 2026-08-20** with Python 3.12.14 on macOS/arm64: every
dependency resolves, V10 passes 9/9, `ruff` and `mypy --strict` are clean, and
pytest runs an async test with no decorator. Reproduce it exactly from
`requirements.lock` rather than from the floors below.

Install the broker SDK **from the vendored wheel**, not from an index:

```bash
.venv/bin/pip install vendor/t_tech_investments-1.49.1-py3-none-any.whl
```

Verify the wheel first if you did not vendor it yourself:

```bash
cd vendor && shasum -a 256 -c SHA256SUMS && cd ..
```

The SDK is not on PyPI — see `vendor/README.md`. Everything else installs
normally:

```bash
.venv/bin/pip install -r requirements.lock
```

`requirements.lock` is the reproducible set. The `requirements-*.txt` files state
intent and floors; install from them only when deliberately moving a version, and
regenerate the lock afterwards.

Activate the venv in every new shell before any `python`/`pip`/`pytest`
command. A `ModuleNotFoundError` almost always means it is not active.

## 3. One-time manual setup

These cannot be automated. Full detail in `technical-spec.md` §1.

1. **Open a separate brokerage account** in the T-Invest app, distinct from your
   main savings, and fund it with the allocated capital **and no more**. This is
   the primary control bounding what a malfunction can cost, and it is a
   requirement, not a precaution.
2. **Issue a full-access API token.** A read-only token cannot place orders.
   Record the account identifier of the account from step 1.
3. **Create the Telegram bot** via BotFather; record its token.
4. **Get your chat identifier** by messaging the bot once and reading the update.
5. **Rent the VPS** (2 vCPU / 2 GB / Ubuntu LTS), install Docker, and confirm
   `timedatectl` reports the clock synchronised.

## 4. Environment variables

```bash
cp .env.example .env && chmod 600 .env
```

Fill it in. `.env` is gitignored and must stay that way — it holds a credential
equivalent to your account password. If it is ever exposed, revoke the token at
the broker immediately; revocation is the only remedy.

Only five variables are required to run the verification suite:
`TINVEST_TOKEN`, `TINVEST_ACCOUNT_ID`, `WATCHLIST`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. Everything else has a working default.

**Start in sandbox.** Set `TRADING_MODE=sandbox` while developing. It selects the
sandbox endpoint only — every call afterwards is identical, so sandbox behaviour
is evidence about live behaviour.

## 5. Database

There is nothing to provision. SQLite creates the file on first run, and
migrations are applied automatically at startup by `db.migrations`. For local
work:

```bash
mkdir -p data && echo "DB_PATH=$(pwd)/data/zarabot.db" >> .env
```

Tests never touch this file — each test builds a temporary database, applies
migrations, and deletes it.

## 6. Broker verification suite

Run this **before writing any application code**. It is the difference between
building on verified ground and building on assumptions.

```bash
set -a && source .env && set +a && PYTHON=.venv/bin/python ./scripts/verify/run_all.sh
```

Twelve scripts, each printing one `PASS`/`FAIL` line and exiting 0 or 1. The
runner stops at the first failure. Notes:

- **V9 fails on macOS** by design — it checks `timedatectl` and `/opt/zarabot/data`.
  Run it on the VPS, or set `ZARABOT_DATA_DIR` to skip past it locally.
- **V2, V6 and V11 place real orders on the sandbox account** at prices that
  cannot fill, and clean up after themselves.
- **V8 asks you to reply `/status`** within 45 seconds.
- Record V7's measured rate limit — it sets `POLL_INTERVAL_SECONDS`, and the
  spec's Dependencies section gets pinned from this run.

## 7. Verification checks — all five must pass

**Check 1 — venv active and correct Python:**
```bash
.venv/bin/python -c "import sys; assert sys.prefix != sys.base_prefix; assert sys.version_info >= (3,12); print('venv OK', sys.version.split()[0])"
```

**Check 2 — dependencies importable, SDK from the vendored wheel:**
```bash
.venv/bin/python -c "import t_tech.invest, aiosqlite, structlog, telegram; from t_tech.invest.schemas import OrderIdType; assert hasattr(OrderIdType,'ORDER_ID_TYPE_REQUEST'); print('deps OK')"
```

**Check 3 — config loads and fails fast on a missing variable:**
```bash
.venv/bin/python -c "import zarabot.config as c; c.load(); print('config OK')"
```
Then confirm the failure path, which matters more than the success path:
```bash
env -u TINVEST_TOKEN .venv/bin/python -c "import zarabot.config as c; c.load()" 2>&1 | grep -q TINVEST_TOKEN && echo "fail-fast OK"
```

**Check 4 — migrations apply to a fresh database:**
```bash
.venv/bin/python -c "
import asyncio, aiosqlite, zarabot.db.migrations as m
async def go():
    async with aiosqlite.connect('/tmp/zarabot-check.db') as db:
        print('schema version', await m.apply(db))
asyncio.run(go())" && rm -f /tmp/zarabot-check.db
```

**Check 5 — test suite collects with no errors:**
```bash
.venv/bin/pytest --collect-only -q | tail -3
```

Checks 3–5 depend on code that does not exist yet — they will fail until the
corresponding modules are built, and that is expected. Checks 1, 2 and the
broker suite in §6 must pass **before** module 1.

## 8. Smoke test

Once Layer 8 is built:

```bash
.venv/bin/python -m zarabot
```

Expect: startup logs, migrations applied, reconciliation reporting no
adjustments, a Telegram message confirming version, mode and halt state. Then
Ctrl-C — a clean shutdown with no orphaned orders confirms graceful shutdown
works. **A start that produces no Telegram message has failed**, whatever the
process status says.
