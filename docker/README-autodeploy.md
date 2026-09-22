# Auto-Deploy System

Automatically rebuilds and redeploys the Docker stack whenever code is pushed to the `main` branch.

The **Makefile** at the project root is the single entry point for every operation — setup, deploy, monitoring, and cleanup. The shell scripts (`deploy.sh`, `setup-autodeploy.sh`, `webhook_server.py`) are the underlying engine; you normally never call them directly.

---

## How it works

```
git push origin main
       │
       ▼
.git/hooks/post-push          ← installed by `make install-hook`; fires on every push
       │
       ▼
scripts/deploy.sh             ← build → rolling restart → health check
       │
       ├── docker compose build          (api, worker, scheduler)
       ├── docker compose up --force-recreate  (one service at a time)
       └── curl /v1/health/live          (polls up to 60 s)
```

For pushes from **remote machines or CI pipelines**, an optional webhook receiver is also provided:

```
GitHub / GitLab
       │  POST /webhook
       ▼
scripts/webhook_server.py     ← verifies HMAC signature, queues job
       │
       ▼
scripts/deploy.sh
```

---

## Quick start

### 1. One-time setup

```bash
make install
```

This does three things in order:

1. Marks `scripts/deploy.sh`, `setup-autodeploy.sh`, and `webhook_server.py` executable.
2. Copies `scripts/post-push.hook` → `.git/hooks/post-push` (if not already present) and `chmod +x` it.
3. Prints a summary confirming the active env file and usage hints.

### 2. Verify everything is wired up

```bash
make status
```

Expected output shows `Git hook: ACTIVE`, `deploy.sh: OK`, the resolved env file, and the live status of each container.

### 3. Push and watch it go

```bash
git push origin main
# Deploy output streams in your terminal automatically
```

---

## All Makefile targets

Run `make help` at any time to see the full list with descriptions.

### Setup & Installation

| Target | Description |
|--------|-------------|
| `make install` | One-time setup: mark scripts executable + install git hook |
| `make install-hook` | Install (or reinstall) the git `post-push` hook only |
| `make uninstall-hook` | Remove the git hook — disables auto-deploy on push |

### Docker Stack

| Target | Description |
|--------|-------------|
| `make up` | Start the full stack (all services) detached |
| `make down` | Stop and remove all containers (volumes are preserved) |
| `make restart` | Restart app services only (api, worker, scheduler) |
| `make build` | Build Docker images using layer cache |
| `make build-no-cache` | Force full Docker rebuild — ignores all cached layers |

### Deploy

| Target | Description |
|--------|-------------|
| `make deploy` | Rebuild + rolling-restart app services (same as a push deploy) |
| `make deploy-no-cache` | Deploy with full cache bust |
| `make deploy-branch BRANCH=<name>` | Deploy a specific branch, e.g. `make deploy-branch BRANCH=staging` |
| `make deploy-services SERVICES=<list>` | Deploy specific services, e.g. `make deploy-services SERVICES=api,worker` |

### Logs & Monitoring

| Target | Description |
|--------|-------------|
| `make logs` | Tail all app service logs (api, worker, scheduler) |
| `make logs-api` | Tail API logs only |
| `make logs-worker` | Tail worker logs only |
| `make logs-scheduler` | Tail scheduler logs only |
| `make logs-db` | Tail postgres and redis logs |
| `make logs-deploy` | Tail the auto-deploy log (`.git/deploy.log`) |
| `make ps` | List all container statuses |
| `make status` | Full system status — hook, scripts, containers, deploy log |
| `make health` | Probe `GET /v1/health/live` and print pass/fail |

### Webhook Server

| Target | Description |
|--------|-------------|
| `make webhook-start` | Start the webhook receiver in Docker (port 9000) |
| `make webhook-stop` | Stop the webhook receiver |
| `make webhook-logs` | Tail webhook container logs |
| `make gen-secret` | Generate a strong `WEBHOOK_SECRET` value |

### Database

| Target | Description |
|--------|-------------|
| `make migrate` | Run Alembic migrations (upgrade to head) |
| `make migrate-down` | Roll back the last migration |
| `make migrate-status` | Show current migration revision |

### Developer Shells

| Target | Description |
|--------|-------------|
| `make shell-api` | Bash shell inside the running API container |
| `make shell-db` | `psql` shell inside the running postgres container |

### Code Quality

| Target | Description |
|--------|-------------|
| `make lint` | Run `ruff check src/` |
| `make fmt` | Auto-format with `ruff format src/` |
| `make typecheck` | Run `mypy src/` |
| `make test` | Run unit tests (no integration) |
| `make test-all` | Run all tests including integration |

### Cleanup

| Target | Description |
|--------|-------------|
| `make clean` | Remove stopped containers + dangling project images |
| `make prune` | **Destructive** — prune all unused Docker resources (asks for confirmation) |

---

## Skipping / overriding a push

```bash
# Skip deploy entirely for this push
SKIP_DEPLOY=1 git push origin main

# Force a full Docker rebuild (no layer cache)
DEPLOY_NO_CACHE=1 git push origin main
```

---

## deploy.sh options (advanced / direct use)

`make deploy` and `make deploy-no-cache` cover the common cases. For fine-grained control you can call the script directly:

```bash
./scripts/deploy.sh [OPTIONS]

  --branch <name>     Only deploy on this branch   (default: main)
  --no-cache          Ignore Docker layer cache
  --skip-health       Skip post-deploy health probe
  --services <list>   Comma-separated services      (default: api,worker,scheduler)
  --env-file <path>   .env file to use              (default: .env.production)
```

---

## Webhook server (remote / CI triggers)

### 1. Generate a secret

```bash
make gen-secret
# Prints: WEBHOOK_SECRET=<hex> — add to .env.production
```

### 2. Start the server

```bash
make webhook-start
# Listening at http://localhost:9000/webhook
# Health at    http://localhost:9000/health
```

### 3. Configure GitHub

1. **Settings → Webhooks → Add webhook**
2. Payload URL: `https://<your-server>:9000/webhook`
3. Content type: `application/json`
4. Secret: the value of `WEBHOOK_SECRET`
5. Events: **Just the push event**

### 4. Configure GitLab

1. **Settings → Webhooks**
2. URL: `https://<your-server>:9000/webhook`
3. Secret token: the value of `WEBHOOK_SECRET`
4. Trigger: **Push events** → branch filter: `main`

### 5. Generic / curl trigger

```bash
curl -s -X POST http://localhost:9000/webhook \
  -H "Authorization: Bearer <WEBHOOK_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"branch":"main","sha":"abc1234","pusher":"you","message":"manual trigger"}'
```

---

## Files

| File | Purpose |
|------|---------|
| `Makefile` | **Primary interface** — all targets documented above |
| `scripts/deploy.sh` | Core deploy logic — build, restart, health check |
| `scripts/post-push.hook` | Hook template copied to `.git/hooks/post-push` by `make install-hook` |
| `scripts/webhook_server.py` | HTTP webhook receiver (GitHub / GitLab / generic) |
| `scripts/setup-autodeploy.sh` | Low-level management CLI (used internally by `make install`) |
| `.git/hooks/post-push` | Active git hook — fires after every `git push` |
| `docker/webhook/docker-compose.webhook.yml` | Runs the webhook server in Docker |

---

## Deploy log

All deploy output is appended to `.git/deploy.log` (excluded from git).

```bash
make logs-deploy        # tails the file live
# or directly:
tail -f .git/deploy.log
```

---

## Slack notifications (optional)

Add to your `.env.production`:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
```

`deploy.sh` posts a success or failure message to that channel after every deploy.

---

## Security notes

- Always run the webhook server behind a TLS-terminating reverse proxy (nginx, Caddy).
- Set `WEBHOOK_SECRET` with `make gen-secret` — never use a short or guessable string.
- The webhook compose file mounts `/var/run/docker.sock`. Restrict firewall access to port 9000.
- The git `post-push` hook only fires on the machine where you run `git push`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Hook fires but deploy is skipped | Current branch doesn't match `DEPLOY_BRANCH` (default: `main`) |
| `permission denied: deploy.sh` | Run `make install` to reset all permissions |
| Hook missing after fresh clone | Run `make install-hook` — git hooks are not tracked by git |
| Health check times out | Check `make logs-api`; ensure `POSTGRES_PASSWORD` is set in env file |
| Webhook returns 401 | `WEBHOOK_SECRET` mismatch between server and GitHub/GitLab settings |
| Webhook returns 429 | A deploy is already running; overlapping deploys are intentionally dropped |
| Docker build fails | Run `make deploy` directly to see the full error output |
