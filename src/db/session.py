"""
Async session management for DATA-SERVICE 2.0.

Provides:
- ``get_async_session`` — an ``asynccontextmanager`` for direct async-with usage
  in service and engine layer code.
- ``get_session`` — a FastAPI dependency that yields an ``AsyncSession`` and
  handles commit/rollback automatically.
- ``get_session_from_engine`` — takes an explicit engine argument; useful in
  tests and startup code that constructs its own engine.

Degraded-mode handling (Requirement 20.2):
- Both helpers gracefully accept ``None`` as the engine value and raise
  ``DatabaseUnavailableError`` so callers can catch it and fall back to cache
  or return a degraded response — the process does not crash.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.db.engine import DatabaseUnavailableError


# ---------------------------------------------------------------------------
# Session factory helpers
# ---------------------------------------------------------------------------


def _make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a reusable ``async_sessionmaker`` bound to *engine*.

    ``expire_on_commit=False`` prevents SQLAlchemy from expiring ORM objects
    after ``session.commit()``, which would require an extra round-trip to
    re-load them — undesirable in an async context.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Context-manager interface (service / engine layer)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_async_session(
    engine: Optional[AsyncEngine],
) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding an ``AsyncSession``.

    Usage::

        async with get_async_session(engine) as session:
            result = await session.execute(select(CandleBar))

    The session is automatically committed on clean exit and rolled back if an
    exception propagates.

    Args:
        engine: A live ``AsyncEngine`` instance.  ``None`` is accepted so that
            callers can pass ``app.state.db_engine`` directly without a
            separate None-guard; ``DatabaseUnavailableError`` is raised in
            that case.

    Yields:
        AsyncSession: A bound session ready for queries.

    Raises:
        DatabaseUnavailableError: When *engine* is ``None`` (database is in
            degraded mode) or when the session cannot be established.
    """
    if engine is None:
        raise DatabaseUnavailableError(
            "Database engine is not available (degraded mode). "
            "Requests requiring persistent data cannot be served."
        )

    factory = _make_session_factory(engine)

    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# FastAPI dependency interface
# ---------------------------------------------------------------------------


async def get_session_from_engine(
    engine: Optional[AsyncEngine],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an ``AsyncSession`` as a FastAPI dependency from an explicit engine.

    This variant is useful in tests where you construct an engine directly
    rather than relying on ``app.state``.

    Args:
        engine: Live ``AsyncEngine``, or ``None`` for degraded mode.

    Yields:
        AsyncSession: A bound session.

    Raises:
        DatabaseUnavailableError: If *engine* is ``None``.
    """
    async with get_async_session(engine) as session:
        yield session


async def get_session(engine: Optional[AsyncEngine] = None) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an ``AsyncSession``.

    In production the engine is injected from ``app.state.db_engine`` via
    a route-level ``Depends`` chain.  Example::

        from fastapi import Depends, Request
        from src.db.session import get_session
        from sqlalchemy.ext.asyncio import AsyncSession

        def _engine_dep(request: Request):
            return request.app.state.db_engine

        @router.get("/example")
        async def example(
            session: AsyncSession = Depends(
                lambda request: get_session(request.app.state.db_engine)
            ),
        ):
            ...

    Args:
        engine: The ``AsyncEngine`` to use.  Defaults to ``None`` (degraded
            mode) but is always supplied in production via the dependency chain.

    Yields:
        AsyncSession

    Raises:
        DatabaseUnavailableError: If *engine* is ``None``.
    """
    async with get_async_session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Convenience: build a session factory from app state
# ---------------------------------------------------------------------------


def session_factory_from_engine(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a reusable session factory.

    Intended for engine-layer code (e.g. bulk upserts, backfill jobs) that
    wants to reuse one factory across many requests rather than reconstructing
    it on every call.
    """
    return _make_session_factory(engine)
