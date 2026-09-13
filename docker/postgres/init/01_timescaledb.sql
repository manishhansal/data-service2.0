-- =============================================================================
-- DATA-SERVICE 2.0 — PostgreSQL initialisation script
-- Runs automatically on first container start via docker-entrypoint-initdb.d/
--
-- This script enables the TimescaleDB extension in the mds database.
-- The Alembic migrations (run separately after the container is healthy) create
-- all tables.  TimescaleDB hypertable promotion is handled by
-- src/db/timescale.py at application startup.
-- =============================================================================

-- Enable TimescaleDB extension.
-- IF NOT EXISTS prevents errors when re-running the init against an existing DB.
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Enable pg_stat_statements for query performance monitoring.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
