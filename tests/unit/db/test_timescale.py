"""
tests/unit/db/test_timescale.py

Unit tests for src/db/timescale.py.

Requirements: 20.6

No live PostgreSQL is required — all database interactions are mocked at
the SQLAlchemy async-connection boundary.

Coverage:
  promote_hypertable()
    - TimescaleDB present → create_hypertable executed, returns True
    - TimescaleDB absent  → graceful INFO log, returns False, no DDL run
    - Detection query raises OperationalError → DatabaseUnavailableError
    - Promotion DDL raises OperationalError  → DatabaseUnavailableError
    - Any unexpected exception              → DatabaseUnavailableError
    - Correct SQL fragments are used (detection query, promotion DDL)
    - commit() is called after successful promotion
    - commit() is NOT called when TimescaleDB is absent
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from sqlalchemy.exc import OperationalError

from src.db.engine import DatabaseUnavailableError
from src.db.timescale import promote_hypertable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_with_connection(
    detect_row: tuple | None,
    execute_raises: Exception | None = None,
    detect_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock AsyncEngine whose connection yields controlled results.

    Args:
        detect_row:      The single row returned by the TimescaleDB detection
                         query.  ``None`` simulates extension not installed.
        execute_raises:  If set, the promotion DDL ``execute`` call raises
                         this exception instead of succeeding.
        detect_raises:   If set, the detection ``execute`` call raises this
                         exception instead of returning a result.
    """
    mock_conn = AsyncMock()
    mock_conn.commit = AsyncMock()

    if detect_raises is not None:
        # The very first execute (detection) raises.
        mock_conn.execute = AsyncMock(side_effect=detect_raises)
    elif execute_raises is not None:
        # Detection succeeds, promotion DDL raises.
        detect_result = MagicMock()
        detect_result.fetchone.return_value = detect_row

        promotion_calls = 0

        async def _execute_side_effect(stmt):  # type: ignore[override]
            nonlocal promotion_calls
            promotion_calls += 1
            if promotion_calls == 1:
                # First call → detection query
                return detect_result
            # Second call → promotion DDL raises
            raise execute_raises

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
    else:
        # Happy path: both detection and promotion succeed.
        detect_result = MagicMock()
        detect_result.fetchone.return_value = detect_row

        promotion_result = MagicMock()

        # Alternate returns: first call = detect, second call = promote.
        mock_conn.execute = AsyncMock(side_effect=[detect_result, promotion_result])

    # Wire the connection into a context-manager-compatible engine.
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.connect = MagicMock(return_value=conn_ctx)
    return engine


# ---------------------------------------------------------------------------
# TimescaleDB present — happy path
# ---------------------------------------------------------------------------


class TestPromoteHypertableTimescalePresent:
    """TimescaleDB is installed: promotion should run and return True."""

    @pytest.mark.asyncio
    async def test_returns_true_when_timescaledb_present(self) -> None:
        """promote_hypertable must return True when the extension is found."""
        engine = _make_engine_with_connection(detect_row=("2.14.2",))
        result = await promote_hypertable(engine)
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_called_twice(self) -> None:
        """Two execute calls expected: detection query + promotion DDL."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        detect_result = MagicMock()
        detect_result.fetchone.return_value = ("2.14.2",)
        promo_result = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=[detect_result, promo_result])

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        await promote_hypertable(engine)

        assert mock_conn.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_commit_called_after_promotion(self) -> None:
        """conn.commit() must be awaited after the promotion DDL succeeds."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        detect_result = MagicMock()
        detect_result.fetchone.return_value = ("2.14.2",)
        promo_result = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=[detect_result, promo_result])

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        await promote_hypertable(engine)

        mock_conn.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detection_sql_targets_pg_extension(self) -> None:
        """The detection query must reference pg_extension and timescaledb."""
        from src.db.timescale import _TIMESCALEDB_DETECT_SQL

        sql_text = str(_TIMESCALEDB_DETECT_SQL)
        assert "pg_extension" in sql_text
        assert "timescaledb" in sql_text

    @pytest.mark.asyncio
    async def test_promotion_sql_targets_candle_bar(self) -> None:
        """The promotion DDL must reference candle_bar and create_hypertable."""
        from src.db.timescale import _PROMOTE_HYPERTABLE_SQL

        sql_text = str(_PROMOTE_HYPERTABLE_SQL)
        assert "candle_bar" in sql_text
        assert "create_hypertable" in sql_text

    @pytest.mark.asyncio
    async def test_promotion_sql_uses_1_day_interval(self) -> None:
        """The chunk_time_interval must be '1 day' per Requirement 20.6."""
        from src.db.timescale import _PROMOTE_HYPERTABLE_SQL

        sql_text = str(_PROMOTE_HYPERTABLE_SQL)
        assert "1 day" in sql_text

    @pytest.mark.asyncio
    async def test_promotion_sql_is_idempotent(self) -> None:
        """The DDL must include if_not_exists => TRUE for idempotency."""
        from src.db.timescale import _PROMOTE_HYPERTABLE_SQL

        sql_text = str(_PROMOTE_HYPERTABLE_SQL)
        assert "if_not_exists" in sql_text

    @pytest.mark.asyncio
    async def test_works_with_various_timescaledb_versions(self) -> None:
        """Promotion should succeed regardless of the detected version string."""
        for version in ("2.10.0", "2.13.1", "2.14.2", "3.0.0"):
            engine = _make_engine_with_connection(detect_row=(version,))
            result = await promote_hypertable(engine)
            assert result is True, f"Expected True for TimescaleDB {version}"


# ---------------------------------------------------------------------------
# TimescaleDB absent — graceful plain-PG fallback
# ---------------------------------------------------------------------------


class TestPromoteHypertableTimescaleAbsent:
    """TimescaleDB is not installed: must return False without raising."""

    @pytest.mark.asyncio
    async def test_returns_false_when_extension_absent(self) -> None:
        """promote_hypertable must return False when pg_extension has no row."""
        engine = _make_engine_with_connection(detect_row=None)
        result = await promote_hypertable(engine)
        assert result is False

    @pytest.mark.asyncio
    async def test_only_detection_query_executed(self) -> None:
        """Exactly one execute call expected — promotion DDL must NOT run."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        detect_result = MagicMock()
        detect_result.fetchone.return_value = None  # extension absent
        mock_conn.execute = AsyncMock(return_value=detect_result)

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        await promote_hypertable(engine)

        # Only the detection query should have been executed.
        assert mock_conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_commit_not_called_when_absent(self) -> None:
        """commit() must NOT be called when TimescaleDB is absent."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        detect_result = MagicMock()
        detect_result.fetchone.return_value = None
        mock_conn.execute = AsyncMock(return_value=detect_result)

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        await promote_hypertable(engine)

        mock_conn.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_raise_when_extension_absent(self) -> None:
        """Plain-PG mode must never raise an exception — graceful fallback."""
        engine = _make_engine_with_connection(detect_row=None)
        # Must complete without raising.
        await promote_hypertable(engine)


# ---------------------------------------------------------------------------
# Error paths — DatabaseUnavailableError propagation
# ---------------------------------------------------------------------------


class TestPromoteHypertableErrorPaths:
    """OperationalError and unexpected exceptions must surface as
    DatabaseUnavailableError so the lifespan handler can enter degraded mode."""

    @pytest.mark.asyncio
    async def test_operational_error_on_detect_raises_db_unavailable(self) -> None:
        """OperationalError during detection → DatabaseUnavailableError."""
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=OperationalError("connection refused", None, None),
        )
        with pytest.raises(DatabaseUnavailableError):
            await promote_hypertable(engine)

    @pytest.mark.asyncio
    async def test_operational_error_on_promotion_raises_db_unavailable(self) -> None:
        """OperationalError during the promotion DDL → DatabaseUnavailableError."""
        engine = _make_engine_with_connection(
            detect_row=("2.14.2",),
            execute_raises=OperationalError("promotion failed", None, None),
        )
        with pytest.raises(DatabaseUnavailableError):
            await promote_hypertable(engine)

    @pytest.mark.asyncio
    async def test_unexpected_exception_on_connect_raises_db_unavailable(self) -> None:
        """Any unexpected exception during connect() → DatabaseUnavailableError."""
        engine = MagicMock()
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("driver crash"))
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        engine.connect = MagicMock(return_value=conn_ctx)

        with pytest.raises(DatabaseUnavailableError):
            await promote_hypertable(engine)

    @pytest.mark.asyncio
    async def test_unexpected_exception_on_execute_raises_db_unavailable(self) -> None:
        """An unexpected non-OperationalError during execute → DatabaseUnavailableError."""
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=ValueError("unexpected driver error"),
        )
        with pytest.raises(DatabaseUnavailableError):
            await promote_hypertable(engine)

    @pytest.mark.asyncio
    async def test_database_unavailable_error_preserves_original_cause(self) -> None:
        """The raised DatabaseUnavailableError must chain the original exception."""
        original = OperationalError("pg down", None, None)
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=original,
        )
        with pytest.raises(DatabaseUnavailableError) as exc_info:
            await promote_hypertable(engine)

        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_error_message_mentions_promotion(self) -> None:
        """The DatabaseUnavailableError message must mention the operation context."""
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=OperationalError("host not found", None, None),
        )
        with pytest.raises(DatabaseUnavailableError, match="TimescaleDB"):
            await promote_hypertable(engine)


# ---------------------------------------------------------------------------
# Degraded-mode integration: simulate server.py lifespan catching the error
# ---------------------------------------------------------------------------


class TestDegradedModeIntegration:
    """Verify the promote_hypertable error contract matches server.py expectations."""

    @pytest.mark.asyncio
    async def test_startup_catches_db_unavailable_from_timescale(self) -> None:
        """server.py lifespan catch block must handle DatabaseUnavailableError
        from promote_hypertable without crashing the process."""
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=OperationalError("pg offline", None, None),
        )

        timescale_promoted = None
        try:
            timescale_promoted = await promote_hypertable(engine)
        except DatabaseUnavailableError:
            pass  # degraded mode — hypertable stays unpromoted

        assert timescale_promoted is None  # promotion did not complete

    @pytest.mark.asyncio
    async def test_absent_timescaledb_allows_startup_to_continue(self) -> None:
        """Plain-PG mode (TimescaleDB absent) must allow normal startup."""
        engine = _make_engine_with_connection(detect_row=None)

        # No exception — platform continues in plain-PG mode.
        result = await promote_hypertable(engine)
        assert result is False
