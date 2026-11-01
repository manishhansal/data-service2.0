# Karpathy-Inspired Coding Guidelines — data-service2.0

> Derived from Andrej Karpathy's observations on LLM coding pitfalls.
> Source: https://github.com/multica-ai/andrej-karpathy-skills
>
> **Tradeoff:** These guidelines bias toward caution over speed.
> For trivial tasks (a typo fix, an obvious one-liner) use judgment.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing anything:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

**In this project specifically:**
- data-service2.0 is the **sole market data provider** for the entire AlphaForge stack. ml-service (8100) and SentinelPulse both consume exclusively from this service — there are no direct provider calls elsewhere. A breaking change here cascades to both downstream consumers.
- This service handles **live broker credentials** (Angel One JWT, Upstox OAuth). Any code that touches `src/auth/` must be treated as security-sensitive. State what auth flow you are modifying before proceeding.
- Provider adapters (Angel One, Upstox, Deribit, Binance) have different rate limits, authentication patterns, and data formats. Do not assume one provider's behavior applies to another.
- `src/core/normaliser.py` produces the canonical OHLCV schema consumed downstream. Any change to the normalized output shape is a breaking change for ml-service feature engineering. Flag it before proceeding.
- Clock skew monitor (`src/monitors/clock_skew_monitor.py`) exists because stale data is indistinguishable from live data without it. Do not bypass or disable timestamp validation logic.

---

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: *Would a senior Python/FastAPI engineer say this is overcomplicated?* If yes, simplify.

**In this project specifically:**
- The provider gateway (`src/providers/gateway.py`) already implements a waterfall (try provider 1 → fallback → fallback). Do not add parallel fetch logic — the waterfall is intentional for rate limit management.
- `src/cache/l1_cache.py` is an in-process L1 cache on top of Redis. Do not add a third cache layer.
- Circuit breakers are per-provider in `src/providers/circuit_breaker.py`. Do not duplicate circuit breaker logic in the API layer.
- `src/core/settings.py` (via `get_settings()`) is the single source of config. Do not add `os.getenv()` calls outside of it.
- Do not add new dependencies to `pyproject.toml` without explicit user approval.
- All new code must pass `ruff` and `mypy --strict` — write types from the start.

---

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style (structlog, pydantic v2, async/await, SQLAlchemy async, `from __future__ import annotations`).
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: *Every changed line should trace directly to the user's request.*

**In this project specifically:**
- `src/middleware/credential_stripper.py` removes sensitive credential data from logs and responses. Do not change its behavior as a side effect — it is a security boundary.
- `src/stores/lineage_store.py` tracks data lineage for audit purposes. Do not skip lineage recording as a performance optimization without explicit instruction.
- `src/forensics/trade_forensics.py` is the audit/forensics module. Treat it like the audit log in ml-service — do not touch unless the task is explicitly about forensics.
- SQLAlchemy model changes in `src/db/models/` require a migration. Note this explicitly before modifying any model.
- `src/providers/adapters/` are isolated per-provider normalizers. Do not add cross-provider logic in an adapter file.
- OpenTelemetry tracing (`src/observability/tracing.py`) instruments all outbound calls. Do not add manual `httpx` calls that bypass the instrumented client.

---

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add a new provider" → "Provider returns normalized OHLCV matching the canonical schema, passes quality alerter validation, has its own circuit breaker — verified by unit tests with fixtures"
- "Fix a data quality issue" → "Write a test with known-bad input → expected quality alert, then make it pass"
- "Add an API endpoint" → "Endpoint returns correct data for happy path and 422 for invalid input — verified by pytest"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

**In this project specifically:**
- Run the relevant test slice before and after any change: `pytest tests/<module> -q --no-cov`
- Provider changes must be verified with a fixture-based test (no live broker calls in CI).
- Auth changes must be verified with mock token fixtures — never use real credentials in tests.
- For schema/normalization changes: verify the output passes the `src/core/validators/` validation layer and that ml-service's `ExpandedFeatureFactory` can consume the new shape.

---

## Architecture Snapshot

| Layer | Path | Notes |
|-------|------|-------|
| API | `src/api/` | FastAPI routers (port 8200) |
| Providers | `src/providers/` | Angel One, Upstox, Deribit, Binance + gateway waterfall |
| Core | `src/core/` | normaliser, pipeline, schemas, validators, parsers |
| Auth | `src/auth/` | Angel One JWT, Upstox OAuth, consumer API key auth |
| Cache | `src/cache/` | Redis L1+L2 (redis_client, l1_cache, cache_manager, event_bus) |
| DB | `src/db/` | SQLAlchemy async + PostgreSQL models |
| Monitors | `src/monitors/` | quality_alerter, clock_skew_monitor |
| Middleware | `src/middleware/` | CORS, rate_limiter, credential_stripper |
| Observability | `src/observability/` | structlog, OpenTelemetry tracing |
| Forensics | `src/forensics/` | trade_forensics — audit/lineage |
| Scheduler | `src/scheduler_jobs/` | Periodic data refresh jobs |
| Workers | `src/worker_tasks/` | Background worker tasks |

**Stack:** Python 3.11 · FastAPI · SQLAlchemy async · Pydantic v2 · Redis · structlog · OpenTelemetry · pytest
**Consumers:** ml-service (8100) · SentinelPulse (3001) · alpha-forge (3000)
**Providers:** Angel One · Upstox · Deribit · Binance
