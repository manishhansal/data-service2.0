# =============================================================================
# DATA-SERVICE 2.0 — Makefile
# =============================================================================
#
# Usage:
#   make help                    Show all available targets
#   make install                 One-time setup (hook + script permissions)
#   make up                      Start full stack (postgres, redis, api, worker, scheduler)
#   make down                    Stop and remove all containers
#   make build                   Build / rebuild Docker images
#   make deploy                  Rebuild + rolling-restart app services
#   make logs                    Tail logs for all app services
#   make status                  Show container and hook status
#
# Push-triggered auto-deploy:
#   make install-hook            Wire up the git post-push hook
#   git push origin main         → automatically calls scripts/deploy.sh
#   SKIP_DEPLOY=1 git push       → skip auto-deploy for this push
#   DEPLOY_NO_CACHE=1 git push   → force full Docker rebuild on push
#
# Webhook server (optional — for remote/CI triggers):
#   make webhook-start           Start the webhook receiver (Docker)
#   make webhook-stop            Stop the webhook receiver
#   make gen-secret              Generate a strong WEBHOOK_SECRET value
#
# =============================================================================

# ── Project metadata ──────────────────────────────────────────────────────────
PROJECT      := data-service
VERSION      := 2.1.0
IMAGE_NAME   := data-service:$(VERSION)

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR  := $(shell pwd)
SCRIPTS_DIR  := $(PROJECT_DIR)/scripts
HOOK_PATH    := $(PROJECT_DIR)/.git/hooks/post-push
HOOK_SOURCE  := $(PROJECT_DIR)/.git/hooks/post-push
DEPLOY_LOG   := $(PROJECT_DIR)/.git/deploy.log
WEBHOOK_COMPOSE := $(PROJECT_DIR)/docker/webhook/docker-compose.webhook.yml

# ── Env file resolution ───────────────────────────────────────────────────────
# Override at the command line:  make up ENV_FILE=.env.staging
# Auto-detection order: .env.local (dev) → .env.production (server) → .env
ENV_FILE     ?= $(or \
  $(if $(wildcard $(PROJECT_DIR)/.env.local),.env.local), \
  $(if $(wildcard $(PROJECT_DIR)/.env.production),.env.production), \
  $(if $(wildcard $(PROJECT_DIR)/.env),.env))

# ── Docker Compose base command ───────────────────────────────────────────────
# Includes --env-file only when a usable file is found.
ifneq ($(ENV_FILE),)
  DC := docker compose --env-file $(ENV_FILE)
else
  DC := docker compose
  $(warning No .env.production / .env.local / .env found — env vars must be pre-exported)
endif

# ── App services (exclude stateful redis/postgres from routine restarts) ──────
APP_SERVICES := api worker scheduler

# ── Colours ───────────────────────────────────────────────────────────────────
CYAN  := \033[0;36m
GREEN := \033[0;32m
YELLOW := \033[1;33m
RED   := \033[0;31m
BOLD  := \033[1m
NC    := \033[0m

# ── Default target ────────────────────────────────────────────────────────────
.DEFAULT_GOAL := help

# Mark all phony targets so Make doesn't look for files with these names
.PHONY: help install install-hook uninstall-hook \
        up down restart build build-no-cache \
        deploy deploy-no-cache \
        logs logs-api logs-worker logs-scheduler logs-db logs-deploy \
        ps status health \
        webhook-start webhook-stop webhook-logs \
        gen-secret \
        migrate shell-api shell-db \
        test lint fmt typecheck \
        backfill-5y backfill-5y-dry backfill-5y-force backfill-5y-idx backfill-5y-eq backfill-5y-fo \
        backfill-5y-fo-universe backfill-5y-all-spot \
        load-fno load-fno-dry fix-fo-gaps \
        load-bhavcopy-5y load-bhavcopy-5y-dry \
        load-bhavcopy-futures load-bhavcopy-options \
        build-continuous-futures build-continuous-futures-dry \
        collect-options-snapshots \
        fix-equity-partials \
        seed-fo-universe seed-fo-universe-dry \
        fix-all-gaps \
        data-report worker-logs catchup-status \
        clean prune

# =============================================================================
# HELP
# =============================================================================

help: ## Show this help message
	@echo ""
	@printf "$(BOLD)$(CYAN)DATA-SERVICE 2.0 — Makefile$(NC)\n"
	@printf "$(CYAN)Image : $(IMAGE_NAME)$(NC)\n"
	@printf "$(CYAN)Env   : $(ENV_FILE)$(NC)\n"
	@echo ""
	@printf "$(BOLD)Usage:$(NC)  make <target>\n\n"
	@awk 'BEGIN {FS = ":.*##"; section=""} \
		/^##@/ { section=$$0; sub(/^##@ /, "", section); printf "\n$(BOLD)%s$(NC)\n", section } \
		/^[a-zA-Z_-]+:.*?##/ { printf "  $(CYAN)%-22s$(NC) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""


# =============================================================================
##@ Setup & Installation
# =============================================================================

install: ## One-time setup: make scripts executable + install git hook
	@printf "$(CYAN)[setup]$(NC) Making scripts executable...\n"
	@chmod +x $(SCRIPTS_DIR)/deploy.sh
	@chmod +x $(SCRIPTS_DIR)/setup-autodeploy.sh
	@chmod +x $(SCRIPTS_DIR)/webhook_server.py
	@$(MAKE) --no-print-directory install-hook
	@printf "$(GREEN)[ok]$(NC)    ENV file  : $(ENV_FILE)\n"
	@echo ""
	@printf "$(GREEN)$(BOLD)✓ Setup complete.$(NC)\n"
	@printf "  Every 'git push origin main' now triggers a rebuild + redeploy.\n"
	@printf "  To skip:           SKIP_DEPLOY=1 git push\n"
	@printf "  To force rebuild:  DEPLOY_NO_CACHE=1 git push\n\n"

install-hook: ## Install the git post-push hook
	@printf "$(CYAN)[setup]$(NC) Installing git post-push hook...\n"
	@if [ ! -d "$(PROJECT_DIR)/.git" ]; then \
		printf "$(RED)[error]$(NC) Not a git repository: $(PROJECT_DIR)\n"; exit 1; \
	fi
	@if [ ! -f "$(HOOK_PATH)" ]; then \
		printf "$(CYAN)[setup]$(NC) Creating post-push hook from template...\n"; \
		cp "$(SCRIPTS_DIR)/post-push.hook" "$(HOOK_PATH)"; \
	fi
	@chmod +x $(HOOK_PATH)
	@printf "$(GREEN)[ok]$(NC)    git post-push hook active: $(HOOK_PATH)\n"

uninstall-hook: ## Remove the git post-push hook (disables auto-deploy on push)
	@if [ -f "$(HOOK_PATH)" ]; then \
		rm "$(HOOK_PATH)"; \
		printf "$(GREEN)[ok]$(NC)    Removed git hook: $(HOOK_PATH)\n"; \
	else \
		printf "$(YELLOW)[warn]$(NC)  Hook not found — nothing to remove\n"; \
	fi


# =============================================================================
##@ Docker Stack
# =============================================================================

up: ## Start the full stack (all services) in detached mode
	@printf "$(CYAN)[docker]$(NC) Starting full stack...\n"
	$(DC) up -d
	@printf "$(GREEN)[ok]$(NC)    Stack is up. API → http://localhost:8200\n"
	@printf "          Run 'make logs' to follow output.\n"

down: ## Stop and remove all containers (data volumes are preserved)
	@printf "$(CYAN)[docker]$(NC) Stopping all containers...\n"
	$(DC) down
	@printf "$(GREEN)[ok]$(NC)    All containers stopped.\n"

restart: ## Restart app services only (api, worker, scheduler)
	@printf "$(CYAN)[docker]$(NC) Restarting app services: $(APP_SERVICES)...\n"
	$(DC) restart $(APP_SERVICES)
	@printf "$(GREEN)[ok]$(NC)    App services restarted.\n"

build: ## Build Docker images (uses layer cache)
	@printf "$(CYAN)[docker]$(NC) Building images (with cache)...\n"
	$(DC) build $(APP_SERVICES)
	@printf "$(GREEN)[ok]$(NC)    Build complete: $(IMAGE_NAME)\n"

build-no-cache: ## Force full Docker rebuild (ignores all layer cache)
	@printf "$(CYAN)[docker]$(NC) Building images (NO cache — full rebuild)...\n"
	$(DC) build --no-cache $(APP_SERVICES)
	@printf "$(GREEN)[ok]$(NC)    Full rebuild complete: $(IMAGE_NAME)\n"


# =============================================================================
##@ Deploy
# =============================================================================

deploy: ## Rebuild + rolling-restart app services (equivalent to a push deploy)
	@printf "$(CYAN)[deploy]$(NC) Running deploy.sh (branch guard + build + restart + health check)...\n"
	@chmod +x $(SCRIPTS_DIR)/deploy.sh
	@$(SCRIPTS_DIR)/deploy.sh

deploy-no-cache: ## Deploy with full Docker cache bust
	@printf "$(CYAN)[deploy]$(NC) Running deploy.sh --no-cache...\n"
	@chmod +x $(SCRIPTS_DIR)/deploy.sh
	@$(SCRIPTS_DIR)/deploy.sh --no-cache

deploy-branch: ## Deploy a specific branch: make deploy-branch BRANCH=feat/my-feature
	@printf "$(CYAN)[deploy]$(NC) Deploying branch: $(BRANCH)...\n"
	@chmod +x $(SCRIPTS_DIR)/deploy.sh
	@$(SCRIPTS_DIR)/deploy.sh --branch $(BRANCH)

deploy-services: ## Deploy specific services only: make deploy-services SERVICES=api,worker
	@printf "$(CYAN)[deploy]$(NC) Deploying services: $(SERVICES)...\n"
	@chmod +x $(SCRIPTS_DIR)/deploy.sh
	@$(SCRIPTS_DIR)/deploy.sh --services $(SERVICES)


# =============================================================================
##@ Logs & Monitoring
# =============================================================================

logs: ## Tail logs for all app services (api, worker, scheduler)
	$(DC) logs -f --tail=100 $(APP_SERVICES)

logs-api: ## Tail API service logs only
	$(DC) logs -f --tail=100 api

logs-worker: ## Tail worker service logs only
	$(DC) logs -f --tail=100 worker

logs-scheduler: ## Tail scheduler service logs only
	$(DC) logs -f --tail=100 scheduler

logs-db: ## Tail postgres and redis logs
	$(DC) logs -f --tail=50 postgres redis

logs-deploy: ## Tail the auto-deploy log (.git/deploy.log)
	@if [ ! -f "$(DEPLOY_LOG)" ]; then \
		printf "$(YELLOW)[warn]$(NC)  No deploy log yet: $(DEPLOY_LOG)\n"; \
	else \
		printf "$(CYAN)[log]$(NC) Tailing $(DEPLOY_LOG) — Ctrl+C to stop\n\n"; \
		tail -n 100 -f $(DEPLOY_LOG); \
	fi

ps: ## List all container statuses
	$(DC) ps

status: ## Show full auto-deploy system status (hook + containers + deploy log)
	@printf "\n$(BOLD)$(CYAN)── Auto-deploy status ──$(NC)\n\n"
	@if [ -x "$(HOOK_PATH)" ]; then \
		printf "  Git hook   : $(GREEN)ACTIVE$(NC)   ($(HOOK_PATH))\n"; \
	else \
		printf "  Git hook   : $(RED)INACTIVE$(NC) (run: make install-hook)\n"; \
	fi
	@if [ -x "$(SCRIPTS_DIR)/deploy.sh" ]; then \
		printf "  deploy.sh  : $(GREEN)OK$(NC)       (executable)\n"; \
	else \
		printf "  deploy.sh  : $(YELLOW)NOT EXEC$(NC) (run: chmod +x scripts/deploy.sh)\n"; \
	fi
	@if [ -n "$(ENV_FILE)" ]; then \
		printf "  Env file   : $(GREEN)$(ENV_FILE)$(NC)\n"; \
	else \
		printf "  Env file   : $(RED)MISSING$(NC)  (no .env.production / .env.local / .env)\n"; \
	fi
	@echo ""
	@if command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1; then \
		HOOK_STATUS=$$(docker inspect --format='{{.State.Status}}' data-service-webhook 2>/dev/null || echo "not running"); \
		if [ "$$HOOK_STATUS" = "running" ]; then \
			printf "  Webhook    : $(GREEN)RUNNING$(NC)  (data-service-webhook)\n"; \
		else \
			printf "  Webhook    : $(YELLOW)$$HOOK_STATUS$(NC)\n"; \
		fi; \
		for svc in api worker scheduler; do \
			CNAME="data-service-$$svc"; \
			CSTATUS=$$(docker inspect --format='{{.State.Status}}' "$$CNAME" 2>/dev/null || echo "not found"); \
			if [ "$$CSTATUS" = "running" ]; then COLOR="$(GREEN)"; else COLOR="$(RED)"; fi; \
			printf "  $$CNAME: $${COLOR}$${CSTATUS}$(NC)\n"; \
		done; \
	else \
		printf "  Docker     : $(YELLOW)not available or not running$(NC)\n"; \
	fi
	@echo ""
	@if [ -f "$(DEPLOY_LOG)" ]; then \
		LINES=$$(wc -l < "$(DEPLOY_LOG)" | tr -d ' '); \
		LAST=$$(tail -1 "$(DEPLOY_LOG)" 2>/dev/null || echo "(empty)"); \
		printf "  Deploy log : $(DEPLOY_LOG) ($$LINES lines)\n"; \
		printf "  Last entry : $$LAST\n"; \
	else \
		printf "  Deploy log : not yet created (appears after first deploy)\n"; \
	fi
	@echo ""

health: ## Probe the API health endpoint
	@printf "$(CYAN)[health]$(NC) Checking http://localhost:8200/v1/health/live ...\n"
	@if curl -sf http://localhost:8200/v1/health/live > /dev/null 2>&1; then \
		printf "$(GREEN)[ok]$(NC)    API is healthy.\n"; \
	else \
		printf "$(RED)[fail]$(NC)  API health check failed. Is the stack running? (make up)\n"; \
		exit 1; \
	fi


# =============================================================================
##@ Webhook Server (remote / CI triggers)
# =============================================================================

webhook-start: ## Start the webhook receiver server (Docker, port 9000)
	@printf "$(CYAN)[webhook]$(NC) Starting webhook server...\n"
	@if [ ! -f "$(WEBHOOK_COMPOSE)" ]; then \
		printf "$(RED)[error]$(NC) Compose file not found: $(WEBHOOK_COMPOSE)\n"; exit 1; \
	fi
	@if [ -z "$(ENV_FILE)" ]; then \
		printf "$(RED)[error]$(NC) No env file found. WEBHOOK_SECRET must be set.\n"; exit 1; \
	fi
	docker compose -f $(WEBHOOK_COMPOSE) --env-file $(ENV_FILE) up -d --build
	@printf "$(GREEN)[ok]$(NC)    Webhook server started.\n"
	@printf "          Health : http://localhost:9000/health\n"
	@printf "          Endpoint: http://localhost:9000/webhook\n"
	@printf "          Configure your GitHub webhook URL to point here.\n"

webhook-stop: ## Stop the webhook receiver server
	@printf "$(CYAN)[webhook]$(NC) Stopping webhook server...\n"
	@if [ -f "$(WEBHOOK_COMPOSE)" ]; then \
		docker compose -f $(WEBHOOK_COMPOSE) down; \
		printf "$(GREEN)[ok]$(NC)    Webhook server stopped.\n"; \
	else \
		printf "$(YELLOW)[warn]$(NC)  Compose file not found: $(WEBHOOK_COMPOSE)\n"; \
	fi

webhook-logs: ## Tail the webhook receiver container logs
	docker compose -f $(WEBHOOK_COMPOSE) logs -f --tail=100

gen-secret: ## Generate a strong WEBHOOK_SECRET value
	@printf "\n$(BOLD)Generated WEBHOOK_SECRET:$(NC)\n\n"
	@SECRET=$$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))'); \
	printf "  WEBHOOK_SECRET=$$SECRET\n\n"; \
	printf "$(YELLOW)Add this line to your $(ENV_FILE) and to your GitHub/GitLab webhook settings.$(NC)\n\n"


# =============================================================================
##@ Database
# =============================================================================

migrate: ## Run Alembic migrations (upgrade to head)
	@printf "$(CYAN)[db]$(NC) Running Alembic migrations...\n"
	$(DC) run --rm api alembic upgrade head
	@printf "$(GREEN)[ok]$(NC)    Migrations applied.\n"

migrate-down: ## Roll back the last Alembic migration
	@printf "$(CYAN)[db]$(NC) Rolling back last migration...\n"
	$(DC) run --rm api alembic downgrade -1

migrate-status: ## Show current Alembic migration state
	$(DC) run --rm api alembic current


# =============================================================================
##@ Developer Shells
# =============================================================================

shell-api: ## Open a bash shell inside the running API container
	$(DC) exec api bash

shell-db: ## Open a psql shell inside the running postgres container
	$(DC) exec postgres psql -U $${POSTGRES_USER:-mds_user} -d $${POSTGRES_DB:-mds}


# =============================================================================
##@ Code Quality
# =============================================================================

lint: ## Run ruff linter
	@printf "$(CYAN)[lint]$(NC) Running ruff...\n"
	ruff check src/

fmt: ## Auto-format code with ruff
	@printf "$(CYAN)[fmt]$(NC) Running ruff format...\n"
	ruff format src/

typecheck: ## Run mypy type checker
	@printf "$(CYAN)[types]$(NC) Running mypy...\n"
	mypy src/

test: ## Run the test suite (unit tests only, no integration)
	@printf "$(CYAN)[test]$(NC) Running pytest (unit tests)...\n"
	@if [ -f "$(ENV_FILE)" ]; then set -a && . ./$(ENV_FILE) && set +a; fi && \
		pytest -m "not integration and not performance" tests/

test-all: ## Run all tests including integration (requires live Redis + PostgreSQL)
	@printf "$(CYAN)[test]$(NC) Running full test suite (integration tests require running stack)...\n"
	@if [ -f "$(ENV_FILE)" ]; then set -a && . ./$(ENV_FILE) && set +a; fi && \
		pytest tests/


# =============================================================================
##@ Data Management
# =============================================================================

backfill-5y: ## Fetch last 5 years of NSE OHLCV data (IDX + EQ + FO) and upsert into the DB
	@printf "$(CYAN)[backfill]$(NC) Starting 5-year Indian market backfill...\n"
	@printf "          Instruments : NSE indices + Nifty 50 equities + NFO futures\n"
	@printf "          Intervals   : 1m 5m 10m 15m 30m 1h 1d 1w 1M\n"
	@printf "          Window      : ~5 years (~1826 calendar days)\n"
	@printf "          Mode        : validate-and-update (existing rows are re-validated)\n"
	@printf "          Rate limit  : concurrency=2, chunk-delay=0.4s, instrument-delay=1s\n"
	@printf "$(YELLOW)          Tip: re-run safely — resumes from Redis checkpoint$(NC)\n\n"
	$(DC) run --rm \
		-e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--concurrency $${CONCURRENCY:-2} \
			--chunk-delay $${CHUNK_DELAY:-0.4} \
			--instrument-delay $${INSTRUMENT_DELAY:-1.0} \
			$${FORCE:+--force} \
			$${SKIP_EXISTING:+--skip-existing} \
			$${DRY_RUN:+--dry-run} \
			$${CLASS:+--class $${CLASS}} \
			$${SYMBOL:+--symbol $${SYMBOL} --class $${CLASS:-EQ}}
	@printf "\n$(GREEN)[ok]$(NC)    5-year backfill complete.\n"

backfill-5y-dry: ## Dry-run the 5-year backfill — print provider plan without touching DB
	@$(MAKE) --no-print-directory backfill-5y DRY_RUN=1

backfill-5y-force: ## Force full 5-year re-fetch (clears Redis checkpoints first)
	@printf "$(YELLOW)[backfill]$(NC) Force mode: Redis checkpoints will be cleared.\n"
	@$(MAKE) --no-print-directory backfill-5y FORCE=1

backfill-5y-idx: ## 5-year backfill for NSE indices only
	@$(MAKE) --no-print-directory backfill-5y CLASS=IDX

backfill-5y-eq: ## 5-year backfill for Nifty 50 equities only
	@$(MAKE) --no-print-directory backfill-5y CLASS=EQ

backfill-5y-fo: ## 5-year backfill for NFO futures only (index + stock futures)
	@$(MAKE) --no-print-directory backfill-5y CLASS=FO

backfill-5y-fo-universe: ## 5-year spot backfill for all 233 F&O-eligible stocks (1d only, Yahoo Finance)
	@printf "$(CYAN)[backfill]$(NC) 5-year spot backfill — full NSE F&O universe (233 stocks)...\n"
	@printf "$(YELLOW)          Uses Yahoo Finance for 1d (no Upstox token required)$(NC)\n\n"
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--class FO_UNIVERSE \
			--intervals 1d \
			--concurrency $${CONCURRENCY:-3} \
			--chunk-delay $${CHUNK_DELAY:-0.3} \
			$${FORCE:+--force}

backfill-5y-all-spot: ## 5-year spot backfill for ALL instruments (Nifty50 + indices + F&O universe)
	@$(MAKE) --no-print-directory backfill-5y CLASS=ALL_SPOT


# =============================================================================
##@ F&O Instrument Master
# =============================================================================

load-fno: ## Download Angel One scrip master and populate instrument_master + provider mappings
	@printf "$(CYAN)[fno]$(NC) Loading F&O instruments from Angel One scrip master...\n"
	@printf "          Source  : https://margincalculator.angelbroking.com (public, no auth)\n"
	@printf "          Populates: instrument_master (expiry, underlying, tokens)\n"
	@printf "                     instrument_provider_mapping (angel_one + upstox per contract)\n"
	@printf "$(YELLOW)          Safe to re-run — fully idempotent (ON CONFLICT DO UPDATE)$(NC)\n\n"
	$(DC) run --rm \
		-e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fno_instrument_master.py
	@printf "\n$(GREEN)[ok]$(NC)    F&O instrument master loaded.\n"

load-fno-dry: ## Dry-run the F&O instrument load — print stats without writing to DB
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fno_instrument_master.py --dry-run

fix-fo-gaps: ## Load F&O instruments then re-run the FO backfill to fill gaps
	@printf "$(CYAN)[fix-fo]$(NC) Step 1/2: Loading F&O instrument master...\n"
	@$(MAKE) --no-print-directory load-fno
	@printf "\n$(CYAN)[fix-fo]$(NC) Step 2/2: Re-running 5-year FO backfill...\n"
	@$(MAKE) --no-print-directory backfill-5y-fo


# =============================================================================
##@ ML Data — Bhavcopy, Continuous Futures, Options Chain
# =============================================================================

load-bhavcopy-5y: ## Download NSE F&O bhavcopy (5 years) → futures_candle + options_candle
	@printf "$(CYAN)[bhavcopy]$(NC) Loading 5-year NSE F&O bhavcopy...\n"
	@printf "          Source : https://nsearchives.nseindia.com/content/fo/ (public)\n"
	@printf "          Loads  : futures_candle + options_candle at 1d resolution\n"
	@printf "          Time   : ~3–5 hours for full 5-year range\n"
	@printf "$(YELLOW)          Safe to re-run — idempotent ON CONFLICT DO UPDATE$(NC)\n\n"
	$(DC) run --rm \
		-e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fo_bhavcopy_5y.py \
			$${FROM_DATE:+--from-date $${FROM_DATE}} \
			$${TO_DATE:+--to-date $${TO_DATE}} \
			$${SYMBOL:+--symbol $${SYMBOL}} \
			--delay $${DELAY:-0.5}
	@printf "\n$(GREEN)[ok]$(NC)    Bhavcopy load complete.\n"

load-bhavcopy-5y-dry: ## Dry-run the bhavcopy load — print plan without downloading
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fo_bhavcopy_5y.py --dry-run

load-bhavcopy-futures: ## Load bhavcopy futures only (faster — skips options)
	@$(MAKE) --no-print-directory load-bhavcopy-5y EXTRA_FLAGS=--futures-only
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fo_bhavcopy_5y.py \
			--futures-only \
			$${FROM_DATE:+--from-date $${FROM_DATE}} \
			$${TO_DATE:+--to-date $${TO_DATE}} \
			$${SYMBOL:+--symbol $${SYMBOL}} \
			--delay $${DELAY:-0.5}

load-bhavcopy-options: ## Load bhavcopy options only
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/load_fo_bhavcopy_5y.py \
			--options-only \
			$${FROM_DATE:+--from-date $${FROM_DATE}} \
			$${TO_DATE:+--to-date $${TO_DATE}} \
			$${SYMBOL:+--symbol $${SYMBOL}} \
			--delay $${DELAY:-0.5}

build-continuous-futures: ## Build Panama-adjusted continuous futures series → continuous_futures table
	@printf "$(CYAN)[continuous]$(NC) Building Panama-adjusted continuous futures...\n"
	@printf "          Prereq : futures_candle must be populated (run load-bhavcopy-5y first)\n"
	@printf "          Output : continuous_futures table, one row per (underlying, date)\n\n"
	$(DC) run --rm \
		-e APP_ENV=$${APP_ENV:-local} \
		api python scripts/build_continuous_futures.py \
			$${SYMBOL:+--symbol $${SYMBOL}}
	@printf "\n$(GREEN)[ok]$(NC)    Continuous futures series built.\n"

build-continuous-futures-dry: ## Dry-run the continuous futures builder
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/build_continuous_futures.py --dry-run \
			$${SYMBOL:+--symbol $${SYMBOL}}

collect-options-snapshots: ## Collect live option chain + IV + Greeks snapshots from Upstox
	@printf "$(CYAN)[options]$(NC) Collecting option chain snapshots...\n"
	@printf "          Requires: UPSTOX_ACCESS_TOKEN (expires daily)\n"
	@printf "          Stores  : option_chain_snapshot + option_chain_contract\n"
	@printf "$(YELLOW)          Schedule this at 09:20, 12:00, 15:29 IST for IV time series$(NC)\n\n"
	$(DC) run --rm \
		-e APP_ENV=$${APP_ENV:-local} \
		api python scripts/collect_options_chain_snapshots.py \
			$${SYMBOLS:+--symbols $${SYMBOLS}} \
			$${EXPIRY:+--expiry $${EXPIRY}}
	@printf "\n$(GREEN)[ok]$(NC)    Options snapshots collected.\n"

fix-equity-partials: ## Re-run 1w/1M backfill for BAJFINANCE, KOTAKBANK, NESTLEIND, GRASIM
	@printf "$(CYAN)[fix-eq]$(NC) Fixing partial equity intervals (1w, 1M)...\n"
	@printf "          Symbols: BAJFINANCE KOTAKBANK NESTLEIND GRASIM\n"
	@printf "$(YELLOW)          Requires: UPSTOX_ACCESS_TOKEN refreshed in .env.local$(NC)\n\n"
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--symbol BAJFINANCE --class EQ --intervals 1w 1M --force
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--symbol KOTAKBANK --class EQ --intervals 1w 1M --force
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--symbol NESTLEIND --class EQ --intervals 1w 1M --force
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/backfill_india_5y.py \
			--symbol GRASIM --class EQ --intervals 1d --force
	@printf "\n$(GREEN)[ok]$(NC)    Equity partial intervals fixed.\n"

seed-fo-universe: ## Populate fo_universe table from DB history + Angel One scrip master
	@printf "$(CYAN)[fo_universe]$(NC) Seeding F&O universe master table...\n"
	@printf "          Sources: options_candle, futures_candle, Angel One scrip master\n"
	@printf "$(YELLOW)          Safe to re-run — idempotent ON CONFLICT DO UPDATE$(NC)\n\n"
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/seed_fo_universe.py
	@printf "\n$(GREEN)[ok]$(NC)    fo_universe seeded.\n"

seed-fo-universe-dry: ## Dry-run the fo_universe seed — show what would be inserted
	$(DC) run --rm -e APP_ENV=$${APP_ENV:-local} \
		api python scripts/seed_fo_universe.py --dry-run

fix-all-gaps: ## Run the full ML gap remediation sequence (takes several hours)	@printf "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════╗$(NC)\n"
	@printf "$(BOLD)$(CYAN)║   ML DATA GAP REMEDIATION — fix-all-gaps                ║$(NC)\n"
	@printf "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════╝$(NC)\n"
	@printf "\n$(YELLOW)Steps to run:$(NC)\n"
	@printf "  1. migrate              — apply DB schema (continuous_futures table)\n"
	@printf "  2. fix-equity-partials  — fill BAJFINANCE/KOTAKBANK/NESTLEIND/GRASIM gaps\n"
	@printf "  3. load-bhavcopy-5y     — 5y NSE F&O bhavcopy → futures_candle + options_candle\n"
	@printf "  4. build-continuous-futures — Panama-adjusted series\n"
	@printf "\n$(YELLOW)Estimated total time: 4–6 hours$(NC)\n\n"
	@printf "$(CYAN)[step 1/4]$(NC) Running DB migrations...\n"
	@$(MAKE) --no-print-directory migrate
	@printf "\n$(CYAN)[step 2/4]$(NC) Fixing equity partial intervals...\n"
	@$(MAKE) --no-print-directory fix-equity-partials
	@printf "\n$(CYAN)[step 3/4]$(NC) Loading 5-year bhavcopy (futures only first)...\n"
	@$(MAKE) --no-print-directory load-bhavcopy-futures
	@printf "\n$(CYAN)[step 4/4]$(NC) Building continuous futures series...\n"
	@$(MAKE) --no-print-directory build-continuous-futures
	@printf "\n$(GREEN)$(BOLD)✓ fix-all-gaps complete.$(NC)\n"
	@printf "  Run 'make data-report' to see updated row counts.\n\n"

data-report: ## Print ML data status — row counts and date ranges per table
	@printf "$(CYAN)[report]$(NC) ML Data Status Report\n"
	@printf "$(CYAN)─────────────────────────────────────────────────────────$(NC)\n"
	$(DC) exec -T postgres psql -U $${POSTGRES_USER:-mds_user} -d $${POSTGRES_DB:-mds} \
		--no-psqlrc -P pager=off -c \
		"SELECT tbl, interval_str, \
		  to_char(rows,'999,999,999') AS rows, \
		  first_date, last_date, instruments \
		 FROM ( \
		   SELECT 'equity_candle' AS tbl, interval_str, COUNT(*) AS rows, \
		     MIN(time)::date AS first_date, MAX(time)::date AS last_date, \
		     COUNT(DISTINCT instrument_id) AS instruments \
		   FROM equity_candle GROUP BY interval_str \
		   UNION ALL \
		   SELECT 'futures_candle', interval_str, COUNT(*), \
		     MIN(time)::date, MAX(time)::date, COUNT(DISTINCT instrument_id) \
		   FROM futures_candle GROUP BY interval_str \
		   UNION ALL \
		   SELECT 'options_candle', interval_str, COUNT(*), \
		     MIN(time)::date, MAX(time)::date, COUNT(DISTINCT instrument_id) \
		   FROM options_candle GROUP BY interval_str \
		   UNION ALL \
		   SELECT 'continuous_futures', '1d', COUNT(*), \
		     MIN(date), MAX(date), COUNT(DISTINCT underlying_id) \
		   FROM continuous_futures \
		 ) t ORDER BY tbl, interval_str;"

worker-logs: ## Tail the worker container logs (shows OHLCV catch-up activity)
	$(DC) logs -f --tail=100 worker

catchup-status: ## Show OHLCV catch-up status — last checkpoint per interval
	@printf "$(CYAN)[catchup]$(NC) OHLCV Catch-up Status (latest candle per interval)\n"
	@printf "$(CYAN)─────────────────────────────────────────────────────────$(NC)\n"
	$(DC) exec -T postgres psql -U $${POSTGRES_USER:-mds_user} -d $${POSTGRES_DB:-mds} \
		--no-psqlrc -P pager=off -c \
		"SELECT interval_str, \
		   COUNT(DISTINCT instrument_id) AS instruments, \
		   MAX(time)::date AS latest_candle, \
		   NOW()::date - MAX(time)::date AS days_behind \
		 FROM equity_candle \
		 GROUP BY interval_str \
		 ORDER BY interval_str;"


# =============================================================================
##@ Cleanup
# =============================================================================

clean: ## Remove stopped containers and dangling images for this project
	@printf "$(CYAN)[clean]$(NC) Removing stopped containers...\n"
	$(DC) rm -f
	@printf "$(CYAN)[clean]$(NC) Pruning dangling images...\n"
	docker image prune -f --filter "label=org.opencontainers.image.title=DATA-SERVICE 2.0" 2>/dev/null || true
	@printf "$(GREEN)[ok]$(NC)    Cleanup done.\n"

prune: ## DANGER: remove all unused Docker resources (images, containers, volumes, networks)
	@printf "$(RED)$(BOLD)[warn]$(NC) This will remove ALL unused Docker resources.\n"
	@printf "$(YELLOW)       Data volumes will NOT be deleted (use 'docker volume prune' for that).$(NC)\n"
	@printf "Continue? [y/N] "; read ans; [ "$$ans" = "y" ] || { echo "Aborted."; exit 0; }
	docker system prune -f
	@printf "$(GREEN)[ok]$(NC)    Docker system pruned.\n"
