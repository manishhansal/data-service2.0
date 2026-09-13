"""
Alembic environment configuration for DATA-SERVICE 2.0.

This module runs in two modes:
1. ``offline`` — emits SQL DDL to stdout/file without a live connection.
   Useful for generating migration scripts to review or apply manually.

2. ``online`` — connects to PostgreSQL via the async ``asyncpg`` driver and
   applies migrations in a transaction.

The database URL is read from the ``DATABASE_URL`` environment variable (via
``src.core.settings.Settings``) — it is never hard-coded here (Req 20.7,
Req 19.1).

Async migration support
-----------------------
Alembic's built-in ``run_migrations_online`` is synchronous.  For an async
SQLAlchemy engine (``asyncpg``) we follow the recommended Alembic async
pattern:

    asyncio.run(run_async_migrations())
        └─> engine.connect()  [async context manager]
                └─> connection.run_sync(do_run_migrations)

This lets Alembic's migration runner (which is synchronous) execute inside the
async connection via ``run_sync``.

References:
- https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
- Requirements: 4.2, 10.11, 20.6 (Task 2.2)
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Optional

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so that ``src.*`` is importable when
# this file is executed by the ``alembic`` CLI.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Alembic Config object — gives access to alembic.ini values.
# ---------------------------------------------------------------------------
config = context.config

# ---------------------------------------------------------------------------
# Inject DATABASE_URL from environment into the Alembic config.
#
# We read it here rather than hardcoding it in alembic.ini.  The URL must use
# the ``postgresql+asyncpg://`` dialect prefix for the async engine; we
# rewrite plain ``postgresql://`` URLs automatically to match engine.py's
# _ensure_asyncpg_url logic.
# ---------------------------------------------------------------------------


def _get_database_url() -> str:
    """Resolve and normalise the database URL from the environment.

    Precedence:
    1. ``DATABASE_URL`` environment variable (plain ``postgresql://`` or
       ``postgresql+asyncpg://`` — both accepted).
    2. ``src.core.settings.Settings.database_url`` (same env var, but goes
       through pydantic validation for early error detection).

    For Alembic offline mode we convert the URL to use the synchronous
    ``psycopg2`` dialect (or plain ``postgresql://``) because the offline
    runner does not open a real connection.  For online mode we keep the
    ``asyncpg`` dialect.
    """
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        # Fall back to settings (triggers validation; will raise if missing).
        try:
            from src.core.settings import get_settings  # noqa: PLC0415

            raw = get_settings().database_url
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "DATABASE_URL environment variable is not set and settings "
                "could not be loaded. Cannot run Alembic migrations."
            ) from exc

    # Normalise to asyncpg dialect for online runs.
    if raw.startswith("postgresql://") or raw.startswith("postgres://"):
        raw = "postgresql+asyncpg://" + raw.split("://", 1)[1]

    return raw


def _get_sync_database_url(async_url: str) -> str:
    """Return a synchronous dialect URL for offline DDL generation.

    Alembic's offline mode does not open a real connection, so we convert
    ``postgresql+asyncpg://`` back to plain ``postgresql://`` to avoid
    asyncpg-specific syntax in the generated SQL.
    """
    if async_url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + async_url[len("postgresql+asyncpg://"):]
    return async_url


# ---------------------------------------------------------------------------
# Set up Python logging from alembic.ini [loggers] section.
# ---------------------------------------------------------------------------
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Metadata import for ``--autogenerate`` support.
#
# When running ``alembic revision --autogenerate``, Alembic introspects the
# SQLAlchemy ORM metadata to determine what changed.  We import the shared
# ``Base`` metadata here.
#
# If ORM models are not yet defined (early in the project), ``target_metadata``
# is ``None`` and autogenerate will emit warnings but still work for
# handwritten migrations.
# ---------------------------------------------------------------------------
target_metadata: Optional[object] = None

try:
    from src.db.base import Base  # noqa: PLC0415 — conditional import

    target_metadata = Base.metadata
except ImportError:
    # ORM Base not yet created (e.g. during initial scaffold).  Handwritten
    # migrations (like our initial schema creation) do not need it.
    target_metadata = None


# ---------------------------------------------------------------------------
# Offline migration runner
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout / a file without a live DB connection.

    In offline mode, Alembic generates DDL statements using the configured
    dialect.  No actual connection is made; the SQL can be piped to psql or
    reviewed before applying.
    """
    database_url = _get_database_url()
    # Use synchronous dialect for offline SQL generation.
    sync_url = _get_sync_database_url(database_url)

    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Compare server defaults so autogenerate picks up DEFAULT changes.
        compare_server_defaults=True,
        # Include partial-index WHERE clauses in autogenerate comparisons.
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online (async) migration runner
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
    """Execute pending migrations inside a synchronous connection context.

    This function is called by ``run_sync`` from within the async event loop,
    giving Alembic's synchronous migration runner access to the live
    connection.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Compare server defaults during autogenerate.
        compare_server_defaults=True,
        # Render AS EXECUTE for partial-index predicates.
        include_schemas=False,
        # Wrap the migration in a single transaction.
        transaction_per_migration=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine, connect, and run migrations.

    We use ``async_engine_from_config`` (Alembic's helper that reads from the
    Alembic ``config`` dict) rather than importing the application engine
    factory directly.  This keeps the migration runner self-contained and
    avoids pulling in optional startup dependencies (e.g. Redis) that are not
    needed during migrations.
    """
    database_url = _get_database_url()

    # Build a minimal config dict for async_engine_from_config.
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        # Use NullPool for migrations — each ``alembic upgrade`` invocation
        # should open exactly one connection and close it when done.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online (async) migration execution.

    Alembic calls this function when a live database connection is available.
    We bridge the synchronous Alembic runner into an asyncio event loop here.
    """
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Dispatch based on Alembic's offline/online context.
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
