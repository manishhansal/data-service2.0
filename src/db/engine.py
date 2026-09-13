"""
Async SQLAlchemy engine factory for DATA-SERVICE 2.0.

Creates an ``AsyncEngine`` backed by ``asyncpg`` using ``DATABASE_URL`` from
application settings (Requirement 22.4).

Degraded-mode contract (Requirement 20.2):
- Connection-pool exhaustion → ``PoolExhaustedError`` is raised; the caller
  (typically the lifespan handler in ``server.py``) is expected to set
  ``app.state.db_engine = None`` and continue in degraded mode.
- PostgreSQL unavailability at startup → ``DatabaseUnavailableError`` is
  raised; same degraded-mode handling applies.
- Neither error is fatal: the platform continues serving cached and
  in-memory data while the database is unreachable.

The engine is *not* a singleton here; callers own the lifecycle and must
call ``engine.dispose()`` on shutdown.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from src.core.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions for degraded-mode signalling
# ---------------------------------------------------------------------------


class DatabaseError(Exception):
    """Base class for database-layer errors."""


class DatabaseUnavailableError(DatabaseError):
    """Raised when PostgreSQL is unreachable during engine initialisation.

    The platform enters degraded mode rather than crashing (Requirement 20.2).
    """


class PoolExhaustedError(DatabaseError):
    """Raised when the connection pool is exhausted and no connection can be
    acquired within the configured timeout.

    This is recoverable — callers should back off and retry or serve from
    cache.
    """


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

# asyncpg requires the ``postgresql+asyncpg://`` dialect prefix.
# If the settings URL uses plain ``postgresql://`` or ``postgres://``,
# we rewrite it here so SQLAlchemy routes through asyncpg.
_ASYNCPG_SCHEMES = ("postgresql+asyncpg://",)
_PLAIN_SCHEMES = (
    "postgresql://",
    "postgres://",
)


def _ensure_asyncpg_url(url: str) -> str:
    """Rewrite a plain postgresql:// URL to postgresql+asyncpg://.

    Leaves ``postgresql+asyncpg://`` URLs unchanged.
    """
    for scheme in _ASYNCPG_SCHEMES:
        if url.startswith(scheme):
            return url  # already correct
    for scheme in _PLAIN_SCHEMES:
        if url.startswith(scheme):
            return "postgresql+asyncpg://" + url[len(scheme):]
    # Unknown scheme — return as-is and let SQLAlchemy raise a descriptive error.
    return url


async def create_async_engine_from_settings(settings: "Settings") -> AsyncEngine:
    """Create and verify an ``AsyncEngine`` from application settings.

    Pool configuration is sized for a production Uvicorn worker count of 4
    (``pool_size=5`` per worker, ``max_overflow=10`` for burst headroom).

    Args:
        settings: Validated application settings sourced from environment
            variables (``DATABASE_URL``, etc.).

    Returns:
        A ready-to-use ``AsyncEngine`` instance. The caller is responsible for
        calling ``engine.dispose()`` on shutdown.

    Raises:
        DatabaseUnavailableError: PostgreSQL is unreachable or refuses the
            connection. The platform should enter degraded mode.
        PoolExhaustedError: The connection pool is exhausted (``QueuePool``
            timeout). The platform should back off and retry.
    """
    url = _ensure_asyncpg_url(settings.database_url)

    # echo=True only in development to avoid flooding production logs.
    echo = str(settings.environment).lower() == "development"

    engine = create_async_engine(
        url,
        # Pool sizing: conservative defaults that work for a single process;
        # horizontal scaling distributes load across replicas (Req 20.2).
        pool_size=5,
        max_overflow=10,
        # How long to wait for a connection from the pool before giving up.
        pool_timeout=30,
        # Recycle connections after 1 hour to guard against stale TCP handles.
        pool_recycle=3600,
        # Validate the connection is still live when checked out of the pool.
        pool_pre_ping=True,
        echo=echo,
        # asyncpg-specific: disable prepared-statement cache on first connect
        # to avoid conflicts when multiple engine instances share a PG user.
        connect_args={
            "server_settings": {
                "application_name": "data-service-2.0",
            }
        },
    )

    # Verify connectivity immediately so startup fails fast when PG is down.
    await _verify_connectivity(engine)

    return engine


async def _verify_connectivity(engine: AsyncEngine) -> None:
    """Run a lightweight ``SELECT 1`` to confirm the engine can reach PG.

    Raises:
        DatabaseUnavailableError: if the connection attempt fails.
        PoolExhaustedError: if the pool is exhausted before a connection is
            obtained.
    """
    from sqlalchemy import text  # local import keeps top-level clean

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SATimeoutError as exc:
        # Pool could not hand out a connection within pool_timeout seconds.
        logger.warning("db_pool_exhausted", extra={"error": str(exc)})
        raise PoolExhaustedError(
            "Connection pool exhausted while verifying database connectivity."
        ) from exc
    except OperationalError as exc:
        # Cannot reach PostgreSQL (wrong host, port closed, auth failure, …).
        logger.warning("db_unavailable", extra={"error": str(exc)})
        raise DatabaseUnavailableError(
            f"PostgreSQL is unreachable: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Any other unexpected driver-level error (e.g., asyncpg-specific).
        logger.warning("db_connect_error", extra={"error": str(exc)})
        raise DatabaseUnavailableError(
            f"Unexpected error while connecting to PostgreSQL: {exc}"
        ) from exc
