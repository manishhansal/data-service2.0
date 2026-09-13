-- scripts/promote_timescaledb.sql
--
-- Promotes the `candle_bar` table to a TimescaleDB hypertable partitioned by
-- the `time` column with 1-day chunks (Requirement 20.6).
--
-- This command is idempotent: `if_not_exists => TRUE` means it is safe to run
-- on an already-promoted hypertable (e.g., during a re-deploy or migration
-- replay) without raising an error.
--
-- Prerequisites:
--   1. TimescaleDB extension must be installed in the target database:
--        CREATE EXTENSION IF NOT EXISTS timescaledb;
--   2. The `candle_bar` table must already exist (see Alembic initial migration).
--   3. The table must contain no rows whose `time` column violates hypertable
--      partitioning constraints (not applicable for a fresh schema).
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/promote_timescaledb.sql
--
-- The platform also runs this automatically on startup via src/db/timescale.py
-- and falls back gracefully if TimescaleDB is not available (plain PostgreSQL mode).

SELECT create_hypertable(
    'candle_bar',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE
);
