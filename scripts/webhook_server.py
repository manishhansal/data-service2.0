#!/usr/bin/env python3
"""
webhook_server.py — Lightweight HTTP server that receives GitHub / GitLab
push webhooks and triggers scripts/deploy.sh.

Supports:
  • GitHub  — X-GitHub-Event: push   + HMAC-SHA256 signature verification
  • GitLab  — X-Gitlab-Event: Push Hook + secret token header verification
  • Generic — plain POST with Bearer token

Usage:
  python scripts/webhook_server.py

Environment variables:
  WEBHOOK_HOST        Bind address          (default: 0.0.0.0)
  WEBHOOK_PORT        Bind port             (default: 9000)
  WEBHOOK_SECRET      HMAC / token secret   (REQUIRED — set a strong value)
  DEPLOY_BRANCH       Branch that triggers  (default: main)
  DEPLOY_SCRIPT       Path to deploy.sh     (default: ./scripts/deploy.sh)
  DEPLOY_LOG_FILE     Where to tail logs    (default: .git/deploy.log)
  MAX_QUEUE_DEPTH     Pending deploy limit  (default: 1 — skip if busy)

Security notes:
  • Always run behind a TLS-terminating reverse proxy (nginx/caddy).
  • Set a long random WEBHOOK_SECRET (e.g. openssl rand -hex 32).
  • Bind to localhost and proxy externally, or restrict firewall rules.
"""

import hashlib
import hmac
import json
import logging
import os
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webhook")

# ── Config ────────────────────────────────────────────────────────────────────
HOST            = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
PORT            = int(os.environ.get("WEBHOOK_PORT", "9000"))
SECRET          = os.environ.get("WEBHOOK_SECRET", "")
DEPLOY_BRANCH   = os.environ.get("DEPLOY_BRANCH", "main")
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR     = os.path.dirname(SCRIPT_DIR)
DEPLOY_SCRIPT   = os.environ.get("DEPLOY_SCRIPT", os.path.join(SCRIPT_DIR, "deploy.sh"))
DEPLOY_LOG_FILE = os.environ.get("DEPLOY_LOG_FILE", os.path.join(PROJECT_DIR, ".git", "deploy.log"))
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "1"))


# ── Deploy queue (single worker — prevents overlapping deploys) ───────────────
_deploy_queue: queue.Queue[dict] = queue.Queue(maxsize=MAX_QUEUE_DEPTH)
_deploy_lock = threading.Lock()


def _deploy_worker() -> None:
    """Background thread that drains the deploy queue one job at a time."""
    while True:
        job = _deploy_queue.get()
        try:
            _run_deploy(job)
        except Exception as exc:
            log.error("Deploy worker error: %s", exc)
        finally:
            _deploy_queue.task_done()


def _run_deploy(job: dict) -> None:
    branch  = job.get("branch", DEPLOY_BRANCH)
    pusher  = job.get("pusher", "unknown")
    sha     = job.get("sha", "")
    message = job.get("message", "")

    log.info("▶  Starting deploy | branch=%s sha=%s pusher=%s", branch, sha[:8], pusher)
    log.info("   Commit: %s", message[:80])

    cmd = [DEPLOY_SCRIPT, "--branch", branch]

    with _deploy_lock:
        try:
            with open(DEPLOY_LOG_FILE, "a") as logf:
                result = subprocess.run(
                    cmd,
                    cwd=PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=600,      # 10-minute hard cap
                )
                logf.write(result.stdout or "")

            if result.returncode == 0:
                log.info("✓  Deploy succeeded | branch=%s sha=%s", branch, sha[:8])
            else:
                log.error("✗  Deploy failed (rc=%d) | branch=%s sha=%s",
                          result.returncode, branch, sha[:8])
                if result.stdout:
                    # Print last 20 lines to server log for quick diagnosis
                    tail = result.stdout.strip().splitlines()[-20:]
                    log.error("    --- last 20 lines ---\n%s", "\n".join(tail))

        except subprocess.TimeoutExpired:
            log.error("✗  Deploy timed out after 600s | branch=%s", branch)
        except FileNotFoundError:
            log.error("✗  Deploy script not found: %s", DEPLOY_SCRIPT)


# Start the background worker thread
_worker_thread = threading.Thread(target=_deploy_worker, daemon=True)
_worker_thread.start()


# ── HMAC helpers ──────────────────────────────────────────────────────────────
def _verify_github(body: bytes, sig_header: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header."""
    if not SECRET:
        return True   # no secret configured → open (not recommended)
    if not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _verify_gitlab(token_header: str) -> bool:
    """Verify GitLab's X-Gitlab-Token header."""
    if not SECRET:
        return True
    return hmac.compare_digest(SECRET, token_header)


def _verify_generic(auth_header: str) -> bool:
    """Verify a plain Bearer token."""
    if not SECRET:
        return True
    expected = f"Bearer {SECRET}"
    return hmac.compare_digest(expected, auth_header)


# ── Request handler ───────────────────────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    """Handles incoming webhook POST requests."""

    # Silence the default request logging (we do our own)
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def _send(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, "ok")
        else:
            self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        if self.path not in ("/webhook", "/webhook/"):
            self._send(404, "not found")
            return

        # Read body
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        ip      = self.client_address[0]

        # Detect source & verify signature
        event_type = self.headers.get("X-GitHub-Event", "")
        gitlab_event = self.headers.get("X-Gitlab-Event", "")

        if event_type:                                        # GitHub
            sig = self.headers.get("X-Hub-Signature-256", "")
            if not _verify_github(body, sig):
                log.warning("⚠  GitHub signature invalid from %s", ip)
                self._send(401, "invalid signature")
                return
            job = _parse_github(body)

        elif gitlab_event:                                    # GitLab
            token = self.headers.get("X-Gitlab-Token", "")
            if not _verify_gitlab(token):
                log.warning("⚠  GitLab token invalid from %s", ip)
                self._send(401, "invalid token")
                return
            job = _parse_gitlab(body)

        else:                                                 # Generic
            auth = self.headers.get("Authorization", "")
            if not _verify_generic(auth):
                log.warning("⚠  Generic auth invalid from %s", ip)
                self._send(401, "unauthorized")
                return
            job = _parse_generic(body)

        if job is None:
            self._send(200, "ignored")
            return

        # Only deploy for the configured branch
        if job.get("branch") != DEPLOY_BRANCH:
            log.info("↷  Skipping push to branch '%s' (watching '%s')",
                     job.get("branch"), DEPLOY_BRANCH)
            self._send(200, "ignored — wrong branch")
            return

        # Enqueue
        try:
            _deploy_queue.put_nowait(job)
            log.info("✉  Deploy queued | branch=%s sha=%s from=%s",
                     job.get("branch"), str(job.get("sha", ""))[:8], ip)
            self._send(202, "deploy queued")
        except queue.Full:
            log.warning("⚡  Deploy already in progress — request dropped from %s", ip)
            self._send(429, "deploy already in progress")


# ── Payload parsers ───────────────────────────────────────────────────────────
def _parse_github(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    ref = data.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return None   # tag push or other event — ignore
    return {
        "branch":  ref.removeprefix("refs/heads/"),
        "sha":     data.get("after", ""),
        "pusher":  data.get("pusher", {}).get("name", "unknown"),
        "message": (data.get("head_commit") or {}).get("message", ""),
    }


def _parse_gitlab(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    ref = data.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return None
    commits = data.get("commits", [{}])
    return {
        "branch":  ref.removeprefix("refs/heads/"),
        "sha":     data.get("after", ""),
        "pusher":  data.get("user_name", "unknown"),
        "message": commits[0].get("message", "") if commits else "",
    }


def _parse_generic(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return {
        "branch":  data.get("branch", DEPLOY_BRANCH),
        "sha":     data.get("sha", ""),
        "pusher":  data.get("pusher", "unknown"),
        "message": data.get("message", ""),
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SECRET:
        log.warning("⚠  WEBHOOK_SECRET is not set — all requests will be accepted!")

    log.info("═" * 60)
    log.info("  data-service Webhook Server")
    log.info("  Listening : http://%s:%d/webhook", HOST, PORT)
    log.info("  Branch    : %s", DEPLOY_BRANCH)
    log.info("  Script    : %s", DEPLOY_SCRIPT)
    log.info("  Log file  : %s", DEPLOY_LOG_FILE)
    log.info("═" * 60)

    server = HTTPServer((HOST, PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down webhook server.")
        server.server_close()
