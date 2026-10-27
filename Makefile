# Shriram eStamp – common tasks. Everything runs in the uv-managed .venv
# (`uv sync` creates it, `uv run` uses it – no manual activate needed).
.DEFAULT_GOAL := help
.PHONY: help setup dev lock upgrade login init-db check status reconcile \
        run-dry run-once run test test5 clean

help: ## Show this help, grouped by category
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@ / {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-z-]+:.*## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Setup
setup: ## Create the venv and install all deps (prod + dev)
	uv sync

lock: ## Refresh uv.lock from pyproject.toml
	uv lock

upgrade: ## Upgrade all deps to their latest allowed versions
	uv lock --upgrade && uv sync

clean: ## Remove the venv
	rm -rf .venv

##@ Database & sign-in
login: ## Google OAuth sign-in (saves secrets/token.json)
	uv run python -m estamp login

init-db: ## Create the Postgres schema and tables
	uv run python -m estamp init-db

check: ## check-setup: DB, sign-in, folders, control sheet, config, APIs, alerts
	uv run python -m estamp check-setup

##@ Run
dev: run-dry ## Alias for run-dry: a safe one-cycle preview

run-dry: ## One cycle, dry run – nothing moved or written (safe)
	uv run python -m estamp run-once --dry-run

run-once: ## One cycle, LIVE (respects config.yaml dry_run)
	uv run python -m estamp run-once

run: ## Loop every loop_seconds (refuses while dry_run is on)
	uv run python -m estamp run

##@ Monitor
status: ## Stock + queue summary in the terminal
	uv run python -m estamp status

reconcile: ## Report Inventory the ledger doesn't know + LANs stuck in ERROR
	uv run python -m estamp reconcile

##@ Test
test: ## Run the test suite once
	ESTAMP_TEST_DB_URL=$(ESTAMP_TEST_DB_URL) uv run pytest -q tests/

test5: ## Run the suite 5× in a row (required before committing – CLAUDE.md)
	@for i in 1 2 3 4 5; do \
		echo "--- run $$i/5 ---"; \
		ESTAMP_TEST_DB_URL=$(ESTAMP_TEST_DB_URL) uv run pytest -q tests/ || exit 1; \
	done
