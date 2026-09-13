"""
Unit tests for SlidingWindowRateLimiter and RateLimitMiddleware (Task 13.3).

Requirements: 19.3

Tests are grouped into:
1. SlidingWindowRateLimiter — core logic tests (no HTTP involved)
2. RateLimitMiddleware — ASGI integration tests using httpx AsyncClient

All tests are async to exercise asyncio.Lock behaviour; pytest-asyncio
is configured with asyncio_mode = "auto" in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from src.middleware.rate_limiter import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW_SEC,
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_app(
    limit: int = DEFAULT_LIMIT,
    window_sec: int = DEFAULT_WINDOW_SEC,
    limiter: SlidingWindowRateLimiter | None = None,
    exempt_paths: frozenset[str] | None = None,
) -> tuple[FastAPI, SlidingWindowRateLimiter]:
    """
    Build a minimal FastAPI app wrapped with RateLimitMiddleware.

    Returns both the app and the shared limiter so tests can inspect
    internal state.
    """
    app = FastAPI()
    shared_limiter = limiter or SlidingWindowRateLimiter(limit=limit, window_sec=window_sec)

    app.add_middleware(
        RateLimitMiddleware,
        limit=limit,
        window_sec=window_sec,
        limiter=shared_limiter,
        exempt_paths=exempt_paths,
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/v1/health/live")
    async def health() -> dict:
        return {"status": "alive"}

    return app, shared_limiter


# ──────────────────────────────────────────────────────────────────────────────
# SlidingWindowRateLimiter — unit tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSlidingWindowRateLimiter:
    """Tests for the core limiter class (no HTTP layer)."""

    async def test_first_request_is_allowed(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=5, window_sec=60)
        assert await limiter.is_allowed("alice") is True

    async def test_requests_within_limit_are_all_allowed(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=5, window_sec=60)
        for _ in range(5):
            assert await limiter.is_allowed("bob") is True

    async def test_request_exceeding_limit_is_rejected(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=3, window_sec=60)
        for _ in range(3):
            await limiter.is_allowed("carol")
        # 4th request must be rejected
        assert await limiter.is_allowed("carol") is False

    async def test_different_consumers_are_independent(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=2, window_sec=60)
        # Exhaust alice
        await limiter.is_allowed("alice")
        await limiter.is_allowed("alice")
        assert await limiter.is_allowed("alice") is False

        # Bob is unaffected
        assert await limiter.is_allowed("bob") is True

    async def test_old_requests_expire_from_window(self) -> None:
        """Requests older than window_sec should not count toward the limit."""
        limiter = SlidingWindowRateLimiter(limit=2, window_sec=1)

        # Record 2 requests "2 seconds ago"
        past = time.monotonic() - 2.0
        lock = await limiter._get_lock("dave")
        async with lock:
            limiter._windows["dave"].append(past)
            limiter._windows["dave"].append(past)

        # Now both timestamps are stale; next request should be allowed
        assert await limiter.is_allowed("dave") is True

    async def test_get_usage_counts_current_window(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=10, window_sec=60)
        for _ in range(3):
            await limiter.is_allowed("eve")
        assert await limiter.get_usage("eve") == 3

    async def test_get_usage_zero_for_unknown_consumer(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=10, window_sec=60)
        assert await limiter.get_usage("nonexistent") == 0

    async def test_get_usage_ignores_stale_entries(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=10, window_sec=1)
        past = time.monotonic() - 5.0
        lock = await limiter._get_lock("frank")
        async with lock:
            limiter._windows["frank"].append(past)

        # Stale entry should not appear in usage
        assert await limiter.get_usage("frank", window_sec=1) == 0

    async def test_reset_clears_consumer_window(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=3, window_sec=60)
        for _ in range(3):
            await limiter.is_allowed("grace")
        # Limit exhausted
        assert await limiter.is_allowed("grace") is False

        # After reset, the consumer has a fresh window
        await limiter.reset("grace")
        assert await limiter.is_allowed("grace") is True

    async def test_reset_nonexistent_consumer_is_safe(self) -> None:
        """reset() for a consumer with no history must not raise."""
        limiter = SlidingWindowRateLimiter()
        await limiter.reset("never_seen")  # should not raise

    async def test_get_reset_epoch_returns_future_or_now(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=5, window_sec=60)
        await limiter.is_allowed("heidi")
        reset = await limiter.get_reset_epoch("heidi")
        # Must be at or after the current wall-clock second
        assert reset >= int(time.time())

    async def test_get_reset_epoch_no_history_returns_now(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=5, window_sec=60)
        reset = await limiter.get_reset_epoch("ivan")
        assert abs(reset - int(time.time())) <= 2  # within 2s tolerance

    async def test_per_call_override_limit(self) -> None:
        """is_allowed() honours a per-call limit override."""
        limiter = SlidingWindowRateLimiter(limit=100, window_sec=60)
        # Override limit to 2 for this consumer
        assert await limiter.is_allowed("judy", limit=2) is True
        assert await limiter.is_allowed("judy", limit=2) is True
        assert await limiter.is_allowed("judy", limit=2) is False

    async def test_concurrent_requests_do_not_exceed_limit(self) -> None:
        """
        When many coroutines race to is_allowed() simultaneously, the total
        allowed count must never exceed the configured limit.
        """
        limit = 5
        limiter = SlidingWindowRateLimiter(limit=limit, window_sec=60)

        results = await asyncio.gather(
            *[limiter.is_allowed("concurrent") for _ in range(20)]
        )

        allowed = sum(1 for r in results if r is True)
        assert allowed == limit


# ──────────────────────────────────────────────────────────────────────────────
# RateLimitMiddleware — ASGI integration tests
# ──────────────────────────────────────────────────────────────────────────────


class TestRateLimitMiddleware:
    """Tests for the Starlette middleware layer."""

    # ── Basic allow / deny ────────────────────────────────────────────────

    async def test_request_within_limit_returns_200(self) -> None:
        app, _ = _make_app(limit=5)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ping")
        assert resp.status_code == 200

    async def test_request_exceeding_limit_returns_429(self) -> None:
        app, _ = _make_app(limit=2, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping")
            await client.get("/ping")
            resp = await client.get("/ping")  # 3rd — should be rejected

        assert resp.status_code == 429

    async def test_429_response_body_canonical_error_envelope(self) -> None:
        app, _ = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping")
            resp = await client.get("/ping")

        assert resp.status_code == 429
        body = resp.json()
        assert "error" in body
        error = body["error"]
        assert error["code"] == "RATE_LIMIT_EXCEEDED"
        assert isinstance(error["message"], str) and error["message"]
        assert isinstance(error["retryAfterMs"], int)
        assert error["retryAfterMs"] >= 0

    async def test_429_has_retry_after_header(self) -> None:
        app, _ = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping")
            resp = await client.get("/ping")

        assert resp.status_code == 429
        assert "retry-after" in resp.headers

    # ── Rate-limit headers ────────────────────────────────────────────────

    async def test_allowed_response_has_ratelimit_headers(self) -> None:
        app, _ = _make_app(limit=10, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ping")

        assert resp.status_code == 200
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers

    async def test_x_ratelimit_limit_header_value(self) -> None:
        app, _ = _make_app(limit=42, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ping")

        assert resp.headers["x-ratelimit-limit"] == "42"

    async def test_x_ratelimit_remaining_decrements(self) -> None:
        app, _ = _make_app(limit=5, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/ping")
            r2 = await client.get("/ping")
            r3 = await client.get("/ping")

        assert int(r1.headers["x-ratelimit-remaining"]) == 4
        assert int(r2.headers["x-ratelimit-remaining"]) == 3
        assert int(r3.headers["x-ratelimit-remaining"]) == 2

    async def test_x_ratelimit_remaining_zero_on_429(self) -> None:
        app, _ = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping")
            resp = await client.get("/ping")

        assert resp.status_code == 429
        assert resp.headers["x-ratelimit-remaining"] == "0"

    async def test_x_ratelimit_reset_is_future_epoch(self) -> None:
        app, _ = _make_app(limit=5, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/ping")

        reset_epoch = int(resp.headers["x-ratelimit-reset"])
        assert reset_epoch >= int(time.time())

    # ── Exempt paths ──────────────────────────────────────────────────────

    async def test_health_live_is_exempt_from_rate_limit(self) -> None:
        """
        /v1/health/live must not be rate-limited so that liveness probes
        always succeed even when a misbehaving consumer hammers the service.
        """
        app, _ = _make_app(limit=2, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Exhaust the limit on /ping
            await client.get("/ping")
            await client.get("/ping")
            assert (await client.get("/ping")).status_code == 429

            # /v1/health/live must still return 200
            resp = await client.get("/v1/health/live")
        assert resp.status_code == 200

    async def test_custom_exempt_paths(self) -> None:
        app, _ = _make_app(
            limit=1,
            window_sec=60,
            exempt_paths=frozenset({"/special"}),
        )

        @app.get("/special")
        async def special() -> dict:
            return {"exempt": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # exhaust limit
            await client.get("/ping")
            assert (await client.get("/ping")).status_code == 429
            # exempt path still works
            resp = await client.get("/special")
        assert resp.status_code == 200

    # ── Consumer identity extraction ─────────────────────────────────────

    async def test_x_forwarded_for_is_used_as_consumer_id(self) -> None:
        """
        Two requests from different X-Forwarded-For IPs must be tracked
        independently even if they come from the same ASGI client host.
        """
        # Limit of 1 — first request from each IP is allowed
        app, limiter = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get(
                "/ping", headers={"X-Forwarded-For": "10.0.0.1"}
            )
            r2 = await client.get(
                "/ping", headers={"X-Forwarded-For": "10.0.0.2"}
            )

        assert r1.status_code == 200
        assert r2.status_code == 200

    async def test_x_forwarded_for_first_ip_is_consumer(self) -> None:
        """Only the first IP in X-Forwarded-For (the real client) is used."""
        app, limiter = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get(
                "/ping",
                headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"},
            )
            # Second request with same real client IP → should be rate-limited
            resp = await client.get(
                "/ping",
                headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.2"},
            )

        assert resp.status_code == 429

    async def test_x_real_ip_fallback(self) -> None:
        """X-Real-IP is used when X-Forwarded-For is absent."""
        app, limiter = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping", headers={"X-Real-IP": "9.9.9.9"})
            resp = await client.get("/ping", headers={"X-Real-IP": "9.9.9.9"})

        assert resp.status_code == 429

    # ── Shared limiter state ──────────────────────────────────────────────

    async def test_shared_limiter_state_across_requests(self) -> None:
        """
        The middleware uses a shared limiter; state from request N must
        carry over to request N+1.
        """
        shared = SlidingWindowRateLimiter(limit=3, window_sec=60)
        app, _ = _make_app(limit=3, window_sec=60, limiter=shared)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(3):
                r = await client.get(
                    "/ping", headers={"X-Forwarded-For": "5.5.5.5"}
                )
                assert r.status_code == 200, f"Request {i + 1} should be allowed"

            over_limit = await client.get(
                "/ping", headers={"X-Forwarded-For": "5.5.5.5"}
            )
        assert over_limit.status_code == 429

    # ── Default configuration ─────────────────────────────────────────────

    async def test_default_limit_is_100_per_60s(self) -> None:
        """
        The module-level defaults must be 100 req / 60 s; verify they are
        applied when no explicit limit/window is given.
        """
        assert DEFAULT_LIMIT == 100
        assert DEFAULT_WINDOW_SEC == 60

    async def test_middleware_with_defaults_allows_100_requests(self) -> None:
        """Exactly 100 requests should be allowed; the 101st rejected."""
        app, _ = _make_app()  # uses DEFAULT_LIMIT=100, DEFAULT_WINDOW_SEC=60
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(100):
                r = await client.get(
                    "/ping", headers={"X-Forwarded-For": "7.7.7.7"}
                )
                assert r.status_code == 200, f"Request {i + 1} of 100 was rejected"

            resp = await client.get(
                "/ping", headers={"X-Forwarded-For": "7.7.7.7"}
            )
        assert resp.status_code == 429

    # ── Response body content-type ────────────────────────────────────────

    async def test_429_content_type_is_json(self) -> None:
        app, _ = _make_app(limit=1, window_sec=60)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/ping")
            resp = await client.get("/ping")

        assert resp.status_code == 429
        assert "application/json" in resp.headers.get("content-type", "")
