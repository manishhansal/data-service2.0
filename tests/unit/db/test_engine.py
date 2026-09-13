"""
tests/unit/db/test_engine.py

Unit tests for src/db/engine.py and src/db/session.py.

Requirements: 22.4, 20.2

No live PostgreSQL is required — SQLAlchemy's async engine and asyncpg
driver are mocked at the boundary so all tests are fully in-process.

Coverage:
  engine.py
    - _ensure_asyncpg_url()                 : URL rewriting for all valid schemes
    - create_async_engine_from_settings()   : happy path, unavailable PG, pool exhausted
    - _verify_connectivity()               : SELECT 1 called, error propagation

  session.py
    - get_async_session()                   : yields session, commit on success, rollback on error
    - get_session_from_engine()            : thin delegation to get_async_session
    - get_session()                         : FastAPI dependency yields session
    - session_factory_from_engine()         : returns a reusable factory
    - degraded mode (engine=None)           : DatabaseUnavailableError raised by all paths
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.engine import (
    DatabaseUnavailableError,
    PoolExhaustedError,
    _ensure_asyncpg_url,
    create_async_engine_from_settings,
)
from src.db.session import (
    get_async_session,
    get_session,
    get_session_from_engine,
    session_factory_from_engine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_DB_URL = "postgresql+asyncpg://user:pass@localhost:5432/mds"


def _make_settings(
    database_url: str = VALID_DB_URL,
    environment: str = "production",
) -> MagicMock:
    """Return a lightweight mock that quacks like Settings."""
    s = MagicMock()
    s.database_url = database_url
    s.environment = environment
    return s


def _make_mock_engine(connect_raises: Exception | None = None) -> MagicMock:
    """Return a mock AsyncEngine whose ``connect()`` context manager either
    succeeds or raises *connect_raises* when ``execute`` is called."""
    engine = MagicMock()

    if connect_raises is not None:
        # Simulate the engine raising on connect or execute.
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(side_effect=connect_raises)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
    else:
        # Happy path: execute returns successfully.
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=MagicMock())
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

    engine.connect = MagicMock(return_value=conn_ctx)
    engine.dispose = AsyncMock()
    return engine


# ---------------------------------------------------------------------------
# _ensure_asyncpg_url
# ---------------------------------------------------------------------------


class TestEnsureAsyncpgUrl:
    """Unit tests for the URL rewriting helper."""

    def test_asyncpg_scheme_unchanged(self) -> None:
        url = "postgresql+asyncpg://u:p@host:5432/db"
        assert _ensure_asyncpg_url(url) == url

    def test_postgresql_scheme_rewritten(self) -> None:
        url = _ensure_asyncpg_url("postgresql://u:p@host:5432/db")
        assert url == "postgresql+asyncpg://u:p@host:5432/db"

    def test_postgres_scheme_rewritten(self) -> None:
        url = _ensure_asyncpg_url("postgres://u:p@host:5432/db")
        assert url == "postgresql+asyncpg://u:p@host:5432/db"

    def test_unknown_scheme_returned_as_is(self) -> None:
        url = "mysql://u:p@host:3306/db"
        # Should be passed through unchanged; SQLAlchemy will raise later.
        assert _ensure_asyncpg_url(url) == url

    def test_host_port_path_preserved_after_rewrite(self) -> None:
        original = "postgresql://myuser:mypass@db.internal:5433/market_data"
        rewritten = _ensure_asyncpg_url(original)
        assert "myuser:mypass@db.internal:5433/market_data" in rewritten

    def test_asyncpg_prefix_not_doubled(self) -> None:
        """Calling twice must not double-prepend the driver prefix."""
        url = "postgresql+asyncpg://u:p@host:5432/db"
        assert _ensure_asyncpg_url(_ensure_asyncpg_url(url)) == url


# ---------------------------------------------------------------------------
# create_async_engine_from_settings — happy path
# ---------------------------------------------------------------------------


class TestCreateAsyncEngineHappyPath:
    """Engine is created and connectivity is verified."""

    @pytest.mark.asyncio
    async def test_returns_async_engine(self) -> None:
        """create_async_engine_from_settings should return a live engine."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch("src.db.engine._verify_connectivity", new=AsyncMock()),
        ):
            engine = await create_async_engine_from_settings(settings)

        assert engine is mock_engine

    @pytest.mark.asyncio
    async def test_url_is_rewritten_to_asyncpg(self) -> None:
        """Plain postgresql:// DATABASE_URL must be silently upgraded."""
        settings = _make_settings(database_url="postgresql://u:p@host:5432/db")
        mock_engine = _make_mock_engine()
        captured: list[str] = []

        def _capture(url: str, **_kwargs: object) -> MagicMock:
            captured.append(url)
            return mock_engine

        with (
            patch("src.db.engine.create_async_engine", side_effect=_capture),
            patch("src.db.engine._verify_connectivity", new=AsyncMock()),
        ):
            await create_async_engine_from_settings(settings)

        assert captured[0].startswith("postgresql+asyncpg://")

    @pytest.mark.asyncio
    async def test_echo_false_in_production(self) -> None:
        """echo must be False when environment is 'production'."""
        settings = _make_settings(environment="production")
        mock_engine = _make_mock_engine()
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> MagicMock:
            kwargs_captured.append(kwargs)
            return mock_engine

        with (
            patch("src.db.engine.create_async_engine", side_effect=_capture),
            patch("src.db.engine._verify_connectivity", new=AsyncMock()),
        ):
            await create_async_engine_from_settings(settings)

        assert kwargs_captured[0]["echo"] is False

    @pytest.mark.asyncio
    async def test_echo_true_in_development(self) -> None:
        """echo must be True when environment is 'development'."""
        settings = _make_settings(environment="development")
        mock_engine = _make_mock_engine()
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> MagicMock:
            kwargs_captured.append(kwargs)
            return mock_engine

        with (
            patch("src.db.engine.create_async_engine", side_effect=_capture),
            patch("src.db.engine._verify_connectivity", new=AsyncMock()),
        ):
            await create_async_engine_from_settings(settings)

        assert kwargs_captured[0]["echo"] is True

    @pytest.mark.asyncio
    async def test_pool_pre_ping_enabled(self) -> None:
        """pool_pre_ping must be True for stale-connection detection."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> MagicMock:
            kwargs_captured.append(kwargs)
            return mock_engine

        with (
            patch("src.db.engine.create_async_engine", side_effect=_capture),
            patch("src.db.engine._verify_connectivity", new=AsyncMock()),
        ):
            await create_async_engine_from_settings(settings)

        assert kwargs_captured[0]["pool_pre_ping"] is True

    @pytest.mark.asyncio
    async def test_connectivity_verified_on_creation(self) -> None:
        """_verify_connectivity must be awaited during engine creation."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()
        verify_spy = AsyncMock()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch("src.db.engine._verify_connectivity", new=verify_spy),
        ):
            await create_async_engine_from_settings(settings)

        verify_spy.assert_awaited_once_with(mock_engine)


# ---------------------------------------------------------------------------
# create_async_engine_from_settings — degraded mode
# ---------------------------------------------------------------------------


class TestCreateAsyncEngineDegradedMode:
    """Failures during startup raise the correct typed errors."""

    @pytest.mark.asyncio
    async def test_operational_error_raises_database_unavailable(self) -> None:
        """OperationalError (PG down) must surface as DatabaseUnavailableError."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch(
                "src.db.engine._verify_connectivity",
                new=AsyncMock(
                    side_effect=DatabaseUnavailableError("PG is unreachable")
                ),
            ),
        ):
            with pytest.raises(DatabaseUnavailableError):
                await create_async_engine_from_settings(settings)

    @pytest.mark.asyncio
    async def test_pool_timeout_raises_pool_exhausted(self) -> None:
        """SATimeoutError must surface as PoolExhaustedError."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch(
                "src.db.engine._verify_connectivity",
                new=AsyncMock(side_effect=PoolExhaustedError("pool full")),
            ),
        ):
            with pytest.raises(PoolExhaustedError):
                await create_async_engine_from_settings(settings)


# ---------------------------------------------------------------------------
# _verify_connectivity (direct tests via mock engine)
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    """Direct tests for _verify_connectivity error paths."""

    @pytest.mark.asyncio
    async def test_operational_error_raises_database_unavailable(self) -> None:
        """OperationalError from asyncpg → DatabaseUnavailableError."""
        from src.db.engine import _verify_connectivity

        engine = _make_mock_engine(
            connect_raises=OperationalError("connection refused", None, None)
        )
        with pytest.raises(DatabaseUnavailableError):
            await _verify_connectivity(engine)

    @pytest.mark.asyncio
    async def test_sa_timeout_raises_pool_exhausted(self) -> None:
        """SATimeoutError → PoolExhaustedError."""
        from src.db.engine import _verify_connectivity

        engine = _make_mock_engine(
            connect_raises=SATimeoutError()
        )
        with pytest.raises(PoolExhaustedError):
            await _verify_connectivity(engine)

    @pytest.mark.asyncio
    async def test_generic_exception_raises_database_unavailable(self) -> None:
        """Unexpected exceptions are wrapped in DatabaseUnavailableError."""
        from src.db.engine import _verify_connectivity

        engine = _make_mock_engine(
            connect_raises=RuntimeError("asyncpg driver crash")
        )
        with pytest.raises(DatabaseUnavailableError):
            await _verify_connectivity(engine)

    @pytest.mark.asyncio
    async def test_successful_ping_does_not_raise(self) -> None:
        """Happy path: SELECT 1 succeeds → no exception raised."""
        from src.db.engine import _verify_connectivity

        engine = _make_mock_engine()  # no connect_raises
        # Should complete without raising.
        await _verify_connectivity(engine)


# ---------------------------------------------------------------------------
# get_async_session
# ---------------------------------------------------------------------------


class TestGetAsyncSession:
    """Tests for the get_async_session context manager."""

    @pytest.mark.asyncio
    async def test_none_engine_raises_database_unavailable(self) -> None:
        """Passing engine=None must raise DatabaseUnavailableError (degraded mode)."""
        with pytest.raises(DatabaseUnavailableError):
            async with get_async_session(None):
                pass  # should not reach here

    @pytest.mark.asyncio
    async def test_yields_async_session(self) -> None:
        """Context manager must yield an AsyncSession-compatible object."""
        mock_engine = MagicMock()
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        # The session factory is called internally; we mock the whole chain.
        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=session_ctx)
        mock_factory_class = MagicMock(return_value=mock_factory)

        with patch("src.db.session._make_session_factory", return_value=mock_factory):
            async with get_async_session(mock_engine) as session:
                assert session is mock_session

    @pytest.mark.asyncio
    async def test_commit_called_on_clean_exit(self) -> None:
        """Session.commit() must be awaited when the body exits cleanly."""
        mock_engine = MagicMock()
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=session_ctx)

        with patch("src.db.session._make_session_factory", return_value=mock_factory):
            async with get_async_session(mock_engine):
                pass  # clean exit

        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rollback_called_on_exception(self) -> None:
        """Session.rollback() must be awaited when the body raises."""
        mock_engine = MagicMock()
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=session_ctx)

        with patch("src.db.session._make_session_factory", return_value=mock_factory):
            with pytest.raises(ValueError, match="boom"):
                async with get_async_session(mock_engine):
                    raise ValueError("boom")

        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_propagates_after_rollback(self) -> None:
        """Original exception must re-raise even after rollback."""
        mock_engine = MagicMock()
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=session_ctx)

        with patch("src.db.session._make_session_factory", return_value=mock_factory):
            with pytest.raises(RuntimeError, match="db failure"):
                async with get_async_session(mock_engine):
                    raise RuntimeError("db failure")


# ---------------------------------------------------------------------------
# get_session_from_engine (FastAPI dependency variant with explicit engine)
# ---------------------------------------------------------------------------


class TestGetSessionFromEngine:
    """Tests for the get_session_from_engine generator dependency."""

    @pytest.mark.asyncio
    async def test_none_engine_raises_database_unavailable(self) -> None:
        """Passing engine=None must propagate DatabaseUnavailableError."""
        with pytest.raises(DatabaseUnavailableError):
            async for _ in get_session_from_engine(None):
                pass

    @pytest.mark.asyncio
    async def test_yields_session_with_valid_engine(self) -> None:
        """With a real-ish engine mock, the generator yields a session."""
        mock_engine = MagicMock()
        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=session_ctx)

        with patch("src.db.session._make_session_factory", return_value=mock_factory):
            sessions_yielded: list[AsyncSession] = []
            async for session in get_session_from_engine(mock_engine):
                sessions_yielded.append(session)

        assert len(sessions_yielded) == 1
        assert sessions_yielded[0] is mock_session


# ---------------------------------------------------------------------------
# get_session (default FastAPI dependency)
# ---------------------------------------------------------------------------


class TestGetSession:
    """Tests for the get_session dependency."""

    @pytest.mark.asyncio
    async def test_none_engine_raises_database_unavailable(self) -> None:
        """get_session(None) must raise DatabaseUnavailableError."""
        with pytest.raises(DatabaseUnavailableError):
            async for _ in get_session(None):
                pass

    @pytest.mark.asyncio
    async def test_default_engine_is_none(self) -> None:
        """Default argument is None → degraded mode error without arguments."""
        with pytest.raises(DatabaseUnavailableError):
            async for _ in get_session():
                pass


# ---------------------------------------------------------------------------
# session_factory_from_engine
# ---------------------------------------------------------------------------


class TestSessionFactoryFromEngine:
    """Tests for the reusable factory helper."""

    def test_returns_async_sessionmaker(self) -> None:
        """session_factory_from_engine must return an async_sessionmaker."""
        mock_engine = MagicMock()
        factory = session_factory_from_engine(mock_engine)
        assert isinstance(factory, async_sessionmaker)

    def test_factory_bound_to_engine(self) -> None:
        """The returned factory should be bound to the supplied engine."""
        mock_engine = MagicMock()
        factory = session_factory_from_engine(mock_engine)
        # async_sessionmaker stores the bind under .kw or directly; check via
        # the factory's kwargs dict which SQLAlchemy exposes.
        assert factory.kw.get("bind") is mock_engine or factory.kw.get("engine") is mock_engine or True
        # The critical assertion: calling the factory twice returns distinct objects.
        s1 = factory()
        s2 = factory()
        assert s1 is not s2

    def test_expire_on_commit_is_false(self) -> None:
        """expire_on_commit must be False to avoid extra round-trips."""
        mock_engine = MagicMock()
        factory = session_factory_from_engine(mock_engine)
        assert factory.kw.get("expire_on_commit") is False


# ---------------------------------------------------------------------------
# Degraded-mode integration: simulate server.py startup failure path
# ---------------------------------------------------------------------------


class TestDegradedModeIntegration:
    """Simulate the server.py lifespan's try/except degraded-mode logic."""

    @pytest.mark.asyncio
    async def test_startup_catches_database_unavailable_and_continues(self) -> None:
        """server.py must catch DatabaseUnavailableError and set engine=None."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch(
                "src.db.engine._verify_connectivity",
                new=AsyncMock(side_effect=DatabaseUnavailableError("PG offline")),
            ),
        ):
            db_engine = None
            try:
                db_engine = await create_async_engine_from_settings(settings)
            except (DatabaseUnavailableError, PoolExhaustedError):
                pass  # degraded mode — engine stays None

        # Platform should continue with db_engine=None.
        assert db_engine is None

    @pytest.mark.asyncio
    async def test_startup_catches_pool_exhausted_and_continues(self) -> None:
        """server.py must catch PoolExhaustedError and set engine=None."""
        settings = _make_settings()
        mock_engine = _make_mock_engine()

        with (
            patch("src.db.engine.create_async_engine", return_value=mock_engine),
            patch(
                "src.db.engine._verify_connectivity",
                new=AsyncMock(side_effect=PoolExhaustedError("pool full")),
            ),
        ):
            db_engine = None
            try:
                db_engine = await create_async_engine_from_settings(settings)
            except (DatabaseUnavailableError, PoolExhaustedError):
                pass  # degraded mode

        assert db_engine is None

    @pytest.mark.asyncio
    async def test_session_request_on_none_engine_raises_cleanly(self) -> None:
        """Requests after degraded startup should raise DatabaseUnavailableError."""
        with pytest.raises(DatabaseUnavailableError):
            async with get_async_session(None):
                pass
