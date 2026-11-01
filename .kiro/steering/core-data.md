---
inclusion: fileMatch
fileMatchPattern: "src/{core,db,monitors,auth}/**"
---

# Core, Data & Auth — Karpathy Guidelines (Service-Specific)

> Applies on top of `karpathy-guidelines.md` when working in `src/core/`, `src/db/`, `src/monitors/`, or `src/auth/`.

## Think Before Coding

- `src/core/normaliser.py` output is the **canonical OHLCV schema** consumed by ml-service's `ExpandedFeatureFactory`. Any change to field names, types, or timestamp semantics is a breaking change for ml-service feature engineering. State the downstream impact before proceeding.
- SQLAlchemy model changes in `src/db/models/` require a migration (`alembic upgrade head`). Confirm the migration is backward-compatible before writing the model change.
- `src/auth/angel_one_jwt.py` and `src/auth/upstox_oauth.py` handle live broker credentials. Token refresh logic failures cause live data outages. State your assumption about token TTL and refresh timing before making changes.
- `clock_skew_monitor.py` raises alerts when a provider's timestamp deviates from wall-clock time. Do not change its tolerance thresholds without understanding the downstream consequence for ml-service PIT validation.

## Simplicity First

- `src/core/validators/` already provides OHLCV and instrument validation. Do not add validation logic in provider adapters — normalize first, validate in core.
- `src/core/pipeline.py` orchestrates the normalize → validate → publish sequence. Do not add steps to it unless the task explicitly requires a new pipeline stage.
- `src/auth/consumer_auth.py` handles inbound API key validation. Do not add per-route auth — all routes go through the middleware.

## Surgical Changes

- Do not change `src/core/schemas/` as a side effect. Schema changes propagate to all consumers.
- `src/monitors/quality_alerter.py` runs continuous quality checks. Do not disable quality checks as a workaround for a data issue — fix the root cause.
- `src/auth/` files handle live credentials — do not add logging of token values even temporarily.

## Goal-Driven Execution

- For normalizer changes: success = all existing normalizer tests pass AND the new output is accepted by `src/core/validators/` validation.
- For auth changes: success = mock token fixtures pass the auth layer AND invalid/expired tokens return 401, verified by pytest.
- For DB model changes: success = migration runs cleanly on a fresh DB AND existing queries in `src/api/` return correct results.
