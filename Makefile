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
	pytest -m "not integration and not performance" tests/

test-all: ## Run all tests including integration (requires live Redis + PostgreSQL)
	@printf "$(CYAN)[test]$(NC) Running full test suite (integration tests require running stack)...\n"
	pytest tests/


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
