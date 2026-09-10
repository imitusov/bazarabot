# The local gate. Same checks CI runs, in about four seconds — fast enough to
# run before every push, which is the point. CI is the backstop for when you
# forget and for agent PRs, not the primary loop.
.PHONY: check lint types test cover docs drift fix all image verify

PY := .venv/bin/python
BIN := .venv/bin

check: lint types test cover docs drift ## everything CI runs
	@echo "── all local gates passed ──"

lint:
	@$(BIN)/ruff check .

types:
	@$(BIN)/mypy zarabot

test:
	@$(BIN)/pytest --cov --cov-report=json -q

cover:
	@$(PY) scripts/ci/check_coverage.py

docs:
	@$(PY) scripts/ci/check_docs.py
	@$(PY) scripts/ci/check_rulebook.py
	@$(PY) scripts/ci/check_latches.py
	@$(PY) scripts/ci/check_events.py
	@$(PY) scripts/ci/check_compose.py
	@$(PY) scripts/ci/check_test_contracts.py

# If tasks/ changes when regenerated, the spec and the tasks an agent is fed
# have diverged — which is how a specification quietly becomes fiction.
drift:
	@$(PY) scripts/make_tasks.py
	@git diff --exit-code tasks/ >/dev/null \
		|| { echo "FAIL tasks/ is stale — commit the regenerated files"; exit 1; }
	@echo "PASS no spec drift"

fix:
	@$(BIN)/ruff check --fix .
	@$(BIN)/ruff format .

image:
	@docker build -t zarabot:local .

# Needs a token and network. Never runs in CI.
verify:
	@set -a && . ./.env && set +a && PYTHON=$(CURDIR)/$(PY) ./scripts/verify/run_all.sh
