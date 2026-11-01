---
inclusion: fileMatch
fileMatchPattern: "src/providers/**"
---

# Providers — Karpathy Guidelines (Service-Specific)

> Applies on top of `karpathy-guidelines.md` when working in `src/providers/`.

## Think Before Coding

- The provider gateway (`gateway.py`) implements a waterfall fallback order. Before changing the order, state which consumer (ml-service or SentinelPulse) depends on which provider and what the fallback consequence is.
- Each provider has its own rate limit, session token TTL, and error codes. Do not apply a fix for one provider to another without verifying it applies.
- `capability_matrix.py` declares what each provider can serve. Before adding a new endpoint to a provider, check if the capability matrix needs updating.
- Circuit breaker thresholds affect live data availability — state the impact before changing any threshold.

## Simplicity First

- The normalize → validate → publish pattern exists in every provider adapter. Do not break it by adding pre/post-processing in the gateway.
- `delta_normaliser.py` and `binance_normaliser.py` are per-provider normalizers. Keep them isolated — cross-provider normalization logic belongs in `src/core/normaliser.py`.
- `circuit_breaker.py` is generic and shared. Do not create a provider-specific circuit breaker subclass unless there's a concrete requirement.

## Surgical Changes

- `binance_persistence.py` and `delta_persistence.py` write to the database. Do not change persistence logic as a side effect of normalization changes.
- Provider stream files (`src/providers/streams/`) manage live WebSocket connections. Do not touch reconnect logic unless the task explicitly involves connection handling.
- Rate limiter (`src/providers/rate_limiter.py`) is separate from the middleware rate limiter. They serve different purposes — do not merge them.

## Goal-Driven Execution

- For provider changes: success = provider returns a `NormalizedOHLCV` dict that passes `src/core/validators/` validation, verified by a pytest fixture test (no live broker connection).
- For circuit breaker changes: success = the breaker opens after N failures and closes after M successful probes, verified by a unit test with a mock provider.
