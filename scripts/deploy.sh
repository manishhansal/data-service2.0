#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Build and redeploy data-service Docker stack
#
# Usage:
#   ./scripts/deploy.sh [OPTIONS]
#
# Options:
#   --branch <name>     Only deploy when on this branch (default: main)
#   --no-cache          Force a full Docker layer rebuild (ignores cache)
#   --skip-health       Skip post-deploy health check
#   --services <list>   Comma-separated services to restart (default: all app services)
#                       e.g. --services api,worker
#   --env-file <path>   Path to .env file (default: .env.production)
#
# Environment variables (can also be set in .env):
#   DEPLOY_BRANCH       Branch that triggers a deploy (default: main)
#   DEPLOY_ENV_FILE     .env file to use (default: .env.production)
#   HEALTH_CHECK_URL    URL for post-deploy health probe
#   SLACK_WEBHOOK_URL   If set, posts deploy status to Slack
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()    { echo -e "${CYAN}[DEPLOY]${NC} $*"; }
ok()     { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}\n"; }

# ── Defaults ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-.env.production}"
HEALTH_CHECK_URL="${HEALTH_CHECK_URL:-http://localhost:8200/v1/health/live}"
HEALTH_TIMEOUT=60          # seconds to wait for healthy state
BUILD_NO_CACHE=false
SKIP_HEALTH=false
SERVICES=""                # empty = all app services

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)    DEPLOY_BRANCH="$2";  shift 2 ;;
    --no-cache)  BUILD_NO_CACHE=true; shift   ;;
    --skip-health) SKIP_HEALTH=true;  shift   ;;
    --services)  SERVICES="$2";       shift 2 ;;
    --env-file)  DEPLOY_ENV_FILE="$2"; shift 2 ;;
    *) error "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Guard: must run from project root ─────────────────────────────────────────
cd "$PROJECT_DIR"
[[ -f "docker-compose.yml" ]] || { error "docker-compose.yml not found in $PROJECT_DIR"; exit 1; }

# ── Guard: only deploy from the correct branch ───────────────────────────────
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
  warn "Current branch is '${CURRENT_BRANCH}', expected '${DEPLOY_BRANCH}'. Skipping deploy."
  exit 0
fi

# ── Env file ──────────────────────────────────────────────────────────────────
if [[ ! -f "$DEPLOY_ENV_FILE" ]]; then
  warn "Env file '$DEPLOY_ENV_FILE' not found. Falling back to .env.local"
  DEPLOY_ENV_FILE=".env.local"
fi
[[ -f "$DEPLOY_ENV_FILE" ]] || { error "No usable env file found."; exit 1; }
ENV_FILE_ARG="--env-file $DEPLOY_ENV_FILE"

# ── Compose target services ───────────────────────────────────────────────────
if [[ -z "$SERVICES" ]]; then
  COMPOSE_SERVICES="api worker scheduler"   # exclude redis/postgres (stateful)
else
  COMPOSE_SERVICES="${SERVICES//,/ }"
fi

# ── Deployment metadata ───────────────────────────────────────────────────────
DEPLOY_START=$(date +%s)
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
GIT_MESSAGE="$(git log -1 --pretty=format:'%s' 2>/dev/null || echo "")"
DEPLOY_TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"

# ── Notification helper ───────────────────────────────────────────────────────
notify_slack() {
  local status="$1" message="$2"
  [[ -z "${SLACK_WEBHOOK_URL:-}" ]] && return 0
  local color
  color=$([ "$status" = "success" ] && echo "good" || echo "danger")
  curl -s -X POST "$SLACK_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "{\"attachments\":[{\"color\":\"${color}\",\"title\":\"data-service deploy — ${status}\",\"text\":\"${message}\",\"footer\":\"Branch: ${DEPLOY_BRANCH} | SHA: ${GIT_SHA}\"}]}" \
    > /dev/null || true
}

# ── Trap for error notification ───────────────────────────────────────────────
trap 'notify_slack "failed" "Deploy failed at step: $BASH_COMMAND"' ERR

# =============================================================================
header "data-service 2.0 — Auto Deploy"
# =============================================================================
log "Timestamp : $DEPLOY_TIMESTAMP"
log "Branch    : $DEPLOY_BRANCH"
log "Git SHA   : $GIT_SHA"
log "Commit    : $GIT_MESSAGE"
log "Env file  : $DEPLOY_ENV_FILE"
log "Services  : $COMPOSE_SERVICES"
[[ "$BUILD_NO_CACHE" == "true" ]] && warn "Cache disabled — full rebuild"

# ── Step 1: Pull latest code ──────────────────────────────────────────────────
header "Step 1 / 5 — Pull latest code"
git fetch --all --prune
git reset --hard "origin/$DEPLOY_BRANCH"
ok "Code updated to $(git rev-parse --short HEAD)"

# ── Step 2: Build Docker images ───────────────────────────────────────────────
header "Step 2 / 5 — Build Docker images"
NO_CACHE_FLAG=""
[[ "$BUILD_NO_CACHE" == "true" ]] && NO_CACHE_FLAG="--no-cache"

docker compose $ENV_FILE_ARG build $NO_CACHE_FLAG $COMPOSE_SERVICES
ok "Images built successfully"

# ── Step 3: Rolling restart of app services ──────────────────────────────────
header "Step 3 / 5 — Rolling restart"
for svc in $COMPOSE_SERVICES; do
  log "Restarting: $svc"
  docker compose $ENV_FILE_ARG up -d --no-deps --force-recreate "$svc"
  ok "$svc restarted"
done

# ── Step 4: Health check ──────────────────────────────────────────────────────
header "Step 4 / 5 — Health check"
if [[ "$SKIP_HEALTH" == "true" ]]; then
  warn "Health check skipped"
else
  log "Waiting for $HEALTH_CHECK_URL (timeout: ${HEALTH_TIMEOUT}s)"
  elapsed=0
  until curl -sf "$HEALTH_CHECK_URL" > /dev/null 2>&1; do
    if (( elapsed >= HEALTH_TIMEOUT )); then
      error "Health check timed out after ${HEALTH_TIMEOUT}s"
      docker compose $ENV_FILE_ARG logs --tail=50 api
      notify_slack "failed" "Health check timed out after ${HEALTH_TIMEOUT}s"
      exit 1
    fi
    sleep 3
    (( elapsed += 3 ))
    log "  ...waiting (${elapsed}s)"
  done
  ok "Service healthy at $HEALTH_CHECK_URL"
fi

# ── Step 5: Cleanup dangling images ──────────────────────────────────────────
header "Step 5 / 5 — Cleanup"
docker image prune -f --filter "label=org.opencontainers.image.title=DATA-SERVICE 2.0" 2>/dev/null || true
ok "Dangling images removed"

# ── Summary ───────────────────────────────────────────────────────────────────
DEPLOY_END=$(date +%s)
DEPLOY_DURATION=$(( DEPLOY_END - DEPLOY_START ))
echo ""
echo -e "${GREEN}${BOLD}✓ Deploy complete${NC}"
echo -e "  Duration : ${DEPLOY_DURATION}s"
echo -e "  SHA      : $GIT_SHA"
echo -e "  Services : $COMPOSE_SERVICES"

notify_slack "success" "Deploy complete in ${DEPLOY_DURATION}s | Commit: ${GIT_MESSAGE}"
