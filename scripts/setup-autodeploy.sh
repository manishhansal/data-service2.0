#!/usr/bin/env bash
# =============================================================================
# setup-autodeploy.sh — One-time setup and management for the auto-deploy system
#
# Commands:
#   install       Wire up the git hook, make scripts executable
#   uninstall     Remove the git hook
#   start-webhook Start the webhook server (Dockerised)
#   stop-webhook  Stop the webhook server
#   status        Show current state of all auto-deploy components
#   logs          Tail the deploy log
#   test-deploy   Trigger a deploy dry-run without a git push
#   gen-secret    Generate a strong WEBHOOK_SECRET and print it
#
# Usage:
#   ./scripts/setup-autodeploy.sh <command>
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()    { echo -e "${CYAN}[setup]${NC} $*"; }
ok()     { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()   { echo -e "${YELLOW}[warn]${NC}  $*"; }
error()  { echo -e "${RED}[error]${NC} $*" >&2; }
header() { echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_PATH="$PROJECT_DIR/.git/hooks/post-push"
HOOK_SOURCE="$PROJECT_DIR/.git/hooks/post-push"   # already written there
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy.sh"
WEBHOOK_SCRIPT="$SCRIPT_DIR/webhook_server.py"
WEBHOOK_COMPOSE="$PROJECT_DIR/docker/webhook/docker-compose.webhook.yml"
DEPLOY_LOG="$PROJECT_DIR/.git/deploy.log"

# ── Helpers ───────────────────────────────────────────────────────────────────
require_git_repo() {
  [[ -d "$PROJECT_DIR/.git" ]] || { error "Not a git repository: $PROJECT_DIR"; exit 1; }
}

require_docker() {
  command -v docker &>/dev/null || { error "Docker not found. Install Docker Desktop."; exit 1; }
  docker info &>/dev/null       || { error "Docker daemon is not running."; exit 1; }
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_install() {
  header "Installing auto-deploy"
  require_git_repo

  # 1. Make all scripts executable
  chmod +x "$DEPLOY_SCRIPT"
  chmod +x "$WEBHOOK_SCRIPT"
  chmod +x "$HOOK_PATH"
  ok "Scripts marked executable"

  # 2. Verify the hook is in place
  if [[ -x "$HOOK_PATH" ]]; then
    ok "git post-push hook is active: $HOOK_PATH"
  else
    error "Hook not found at $HOOK_PATH — was it deleted?"
    exit 1
  fi

  # 3. Check for an env file
  ENV_FILE=""
  for f in .env.production .env.local .env; do
    [[ -f "$PROJECT_DIR/$f" ]] && { ENV_FILE="$f"; break; }
  done
  if [[ -n "$ENV_FILE" ]]; then
    ok "Env file found: $ENV_FILE"
  else
    warn "No env file found (.env.production / .env.local / .env). Deploy will fail without one."
  fi

  # 4. Remind about WEBHOOK_SECRET
  if grep -q "WEBHOOK_SECRET=" "$PROJECT_DIR/.env.production" 2>/dev/null || \
     grep -q "WEBHOOK_SECRET=" "$PROJECT_DIR/.env.local" 2>/dev/null; then
    ok "WEBHOOK_SECRET detected in env file"
  else
    warn "WEBHOOK_SECRET not found in env file."
    warn "Run:  ./scripts/setup-autodeploy.sh gen-secret  — then add it to your .env file"
  fi

  echo ""
  echo -e "${GREEN}${BOLD}✓ Auto-deploy installed.${NC}"
  echo "  Every 'git push origin main' will now trigger a rebuild + redeploy."
  echo "  To skip a push:      SKIP_DEPLOY=1 git push"
  echo "  To force no-cache:   DEPLOY_NO_CACHE=1 git push"
  echo ""
  echo "  For remote triggers, also start the webhook server:"
  echo "    ./scripts/setup-autodeploy.sh start-webhook"
}

cmd_uninstall() {
  header "Uninstalling auto-deploy hook"
  require_git_repo

  if [[ -f "$HOOK_PATH" ]]; then
    rm "$HOOK_PATH"
    ok "Removed git hook: $HOOK_PATH"
  else
    warn "Hook not found — nothing to remove"
  fi
  echo ""
  echo "Auto-deploy hook removed. The deploy.sh and webhook_server.py scripts remain."
}

cmd_start_webhook() {
  header "Starting webhook server"
  require_docker
  [[ -f "$WEBHOOK_COMPOSE" ]] || { error "Compose file not found: $WEBHOOK_COMPOSE"; exit 1; }

  # Detect env file
  ENV_ARG=""
  for f in .env.production .env.local .env; do
    [[ -f "$PROJECT_DIR/$f" ]] && { ENV_ARG="--env-file $PROJECT_DIR/$f"; break; }
  done

  docker compose -f "$WEBHOOK_COMPOSE" $ENV_ARG up -d --build
  ok "Webhook server started"

  PORT="${WEBHOOK_PORT:-9000}"
  echo ""
  echo "  Listening at : http://localhost:${PORT}/webhook"
  echo "  Health check : http://localhost:${PORT}/health"
  echo "  View logs    : docker compose -f docker/webhook/docker-compose.webhook.yml logs -f"
  echo ""
  echo "  Configure your GitHub/GitLab webhook URL to:"
  echo "    https://<your-server>:${PORT}/webhook"
  echo "  Content type : application/json"
  echo "  Events       : Just the push event"
}

cmd_stop_webhook() {
  header "Stopping webhook server"
  require_docker
  [[ -f "$WEBHOOK_COMPOSE" ]] || { error "Compose file not found: $WEBHOOK_COMPOSE"; exit 1; }
  docker compose -f "$WEBHOOK_COMPOSE" down
  ok "Webhook server stopped"
}

cmd_status() {
  header "Auto-deploy status"
  require_git_repo

  echo ""
  # Git hook
  if [[ -x "$HOOK_PATH" ]]; then
    echo -e "  Git hook   : ${GREEN}ACTIVE${NC}   ($HOOK_PATH)"
  else
    echo -e "  Git hook   : ${RED}INACTIVE${NC} ($HOOK_PATH not found or not executable)"
  fi

  # Deploy script
  if [[ -x "$DEPLOY_SCRIPT" ]]; then
    echo -e "  deploy.sh  : ${GREEN}OK${NC}       (executable)"
  else
    echo -e "  deploy.sh  : ${YELLOW}NOT EXEC${NC} (run: chmod +x $DEPLOY_SCRIPT)"
  fi

  # Webhook container
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    STATUS="$(docker inspect --format='{{.State.Status}}' data-service-webhook 2>/dev/null || echo "not running")"
    if [[ "$STATUS" == "running" ]]; then
      echo -e "  Webhook    : ${GREEN}RUNNING${NC}  (container: data-service-webhook)"
    else
      echo -e "  Webhook    : ${YELLOW}${STATUS}${NC}"
    fi

    # App containers
    for svc in api worker scheduler; do
      CNAME="data-service-${svc}"
      CSTATUS="$(docker inspect --format='{{.State.Status}}' "$CNAME" 2>/dev/null || echo "not found")"
      COLOR="$RED"
      [[ "$CSTATUS" == "running" ]] && COLOR="$GREEN"
      echo -e "  $CNAME: ${COLOR}${CSTATUS}${NC}"
    done
  else
    echo -e "  Docker     : ${YELLOW}not available${NC}"
  fi

  # Deploy log
  echo ""
  if [[ -f "$DEPLOY_LOG" ]]; then
    LINES="$(wc -l < "$DEPLOY_LOG" | tr -d ' ')"
    LAST="$(tail -1 "$DEPLOY_LOG" 2>/dev/null || echo "(empty)")"
    echo -e "  Deploy log : $DEPLOY_LOG ($LINES lines)"
    echo -e "  Last entry : $LAST"
  else
    echo -e "  Deploy log : not yet created (will appear after first deploy)"
  fi
  echo ""
}

cmd_logs() {
  header "Deploy log"
  if [[ ! -f "$DEPLOY_LOG" ]]; then
    warn "No deploy log yet: $DEPLOY_LOG"
    exit 0
  fi
  LINES="${1:-100}"
  echo "(showing last $LINES lines — Ctrl+C to stop)"
  echo ""
  tail -n "$LINES" -f "$DEPLOY_LOG"
}

cmd_test_deploy() {
  header "Test deploy (dry-run simulation)"
  require_git_repo

  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
  SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  MSG="$(git log -1 --pretty=format:'%s' 2>/dev/null || echo "")"

  echo ""
  echo "  This will run scripts/deploy.sh right now (no git push needed)."
  echo "  Branch : $BRANCH"
  echo "  SHA    : $SHA"
  echo "  Commit : $MSG"
  echo ""
  read -r -p "  Proceed? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { log "Aborted."; exit 0; }

  "$DEPLOY_SCRIPT"
}

cmd_gen_secret() {
  header "Generate WEBHOOK_SECRET"
  SECRET="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
  echo ""
  echo "  Add this line to your .env.production (or .env.local):"
  echo ""
  echo -e "  ${BOLD}WEBHOOK_SECRET=${SECRET}${NC}"
  echo ""
  warn "Store this value securely — it must match what you enter in GitHub/GitLab webhook settings."
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
cd "$PROJECT_DIR"

COMMAND="${1:-help}"
case "$COMMAND" in
  install)        cmd_install ;;
  uninstall)      cmd_uninstall ;;
  start-webhook)  cmd_start_webhook ;;
  stop-webhook)   cmd_stop_webhook ;;
  status)         cmd_status ;;
  logs)           cmd_logs "${2:-100}" ;;
  test-deploy)    cmd_test_deploy ;;
  gen-secret)     cmd_gen_secret ;;
  help|--help|-h)
    echo ""
    echo -e "${BOLD}usage:${NC} ./scripts/setup-autodeploy.sh <command>"
    echo ""
    echo "  install          Wire up git hook and verify setup"
    echo "  uninstall        Remove the git hook"
    echo "  start-webhook    Start the webhook receiver server (Docker)"
    echo "  stop-webhook     Stop the webhook receiver server"
    echo "  status           Show state of all auto-deploy components"
    echo "  logs [N]         Tail last N lines of deploy log (default: 100)"
    echo "  test-deploy      Trigger deploy.sh manually (no push required)"
    echo "  gen-secret       Generate a strong WEBHOOK_SECRET value"
    echo ""
    ;;
  *) error "Unknown command: $COMMAND"; echo "Run with 'help' for usage."; exit 1 ;;
esac
