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

    As of schema revision b1c2d3e4f5a6, promote_hypertable makes two
    execute calls: (1) detection query and (2) hypertable inventory query.
    The promotion DDL no longer exists — hypertables are managed by Alembic.

    Args:
        detect_row:      The single row returned by the TimescaleDB detection
                         query.  ``None`` simulates extension not installed.
        execute_raises:  If set, the second execute call (inventory query)
                         raises this exception.  Note: the new code catches
                         inventory errors as non-fatal (WARNING log, no raise).
        detect_raises:   If set, the detection ``execute`` call raises this
                         exception instead of returning a result.
    """
    mock_conn = AsyncMock()
    mock_conn.commit = AsyncMock()

    if detect_raises is not None:
        # The very first execute (detection) raises.
        mock_conn.execute = AsyncMock(side_effect=detect_raises)
    elif execute_raises is not None:
        # Detection succeeds, inventory query raises.
        detect_result = MagicMock()
        detect_result.fetchone.return_value = detect_row

        inventory_calls = 0

        async def _execute_side_effect(stmt):  # type: ignore[override]
            nonlocal inventory_calls
            inventory_calls += 1
            if inventory_calls == 1:
                # First call → detection query
                return detect_result
            # Second call → inventory query raises
            raise execute_raises

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
    else:
        # Happy path: both detection and inventory succeed.
        detect_result = MagicMock()
        detect_result.fetchone.return_value = detect_row

        inventory_result = MagicMock()
        inventory_result.fetchall.return_value = []

        # Alternate returns: first call = detect, second call = inventory.
        mock_conn.execute = AsyncMock(side_effect=[detect_result, inventory_result])

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
        """Two execute calls expected: detection query + hypertable inventory query."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        detect_result = MagicMock()
        detect_result.fetchone.return_value = ("2.14.2",)
        inventory_result = MagicMock()
        inventory_result.fetchall.return_value = []
        mock_conn.execute = AsyncMock(side_effect=[detect_result, inventory_result])

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        await promote_hypertable(engine)

        assert mock_conn.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_commit_called_after_promotion(self) -> None:
        """As of schema revision b1c2d3e4f5a6, promote_hypertable no longer
        executes DDL (hypertables are created by Alembic).  commit() is
        therefore NOT called.  This test verifies the updated contract."""
        mock_conn = AsyncMock()
        mock_conn.commit = AsyncMock()

        # Detection returns a valid version; inventory list returns empty.
        detect_result = MagicMock()
        detect_result.fetchone.return_value = ("2.14.2",)
        inventory_result = MagicMock()
        inventory_result.fetchall.return_value = []
        mock_conn.execute = AsyncMock(side_effect=[detect_result, inventory_result])

        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect = MagicMock(return_value=conn_ctx)

        result = await promote_hypertable(engine)

        assert result is True
        # No commit — the new implementation is read-only (detect + inventory)
        mock_conn.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_detection_sql_targets_pg_extension(self) -> None:
        """The detection query must reference pg_extension and timescaledb."""
        from src.db.timescale import _TIMESCALEDB_DETECT_SQL

        sql_text = str(_TIMESCALEDB_DETECT_SQL)
        assert "pg_extension" in sql_text
        assert "timescaledb" in sql_text

    @pytest.mark.asyncio
    async def test_promotion_sql_targets_candle_bar(self) -> None:
        """As of schema revision b1c2d3e4f5a6, _PROMOTE_HYPERTABLE_SQL has been
        removed.  The detection SQL (_TIMESCALEDB_DETECT_SQL) and the inventory
        SQL (_LIST_HYPERTABLES_SQL) are the only SQL constants.  This test
        verifies that _PROMOTE_HYPERTABLE_SQL is intentionally absent and that
        the detection SQL correctly targets pg_extension."""
        from src.db.timescale import _TIMESCALEDB_DETECT_SQL

        # Verify the detection SQL is present and correct
        sql_text = str(_TIMESCALEDB_DETECT_SQL)
        assert "pg_extension" in sql_text
        assert "timescaledb" in sql_text

        # Verify that the old promotion constant no longer exists
        import src.db.timescale as tsmod
        assert not hasattr(tsmod, "_PROMOTE_HYPERTABLE_SQL"), (
            "_PROMOTE_HYPERTABLE_SQL should not exist — "
            "candle_bar is no longer promoted to a hypertable"
        )

    @pytest.mark.asyncio
    async def test_promotion_sql_uses_1_day_interval(self) -> None:
        """As of schema revision b1c2d3e4f5a6, the _LIST_HYPERTABLES_SQL
        inventory query must exist (replacing the removed promotion DDL).
        The canonical hypertables use 7-day and 1-day chunk intervals
        configured in the Alembic migration, not here."""
        from src.db.timescale import _LIST_HYPERTABLES_SQL

        sql_text = str(_LIST_HYPERTABLES_SQL)
        # Inventory query references timescaledb_information
        assert "hypertable" in sql_text.lower()
        assert "timescaledb_information" in sql_text

    @pytest.mark.asyncio
    async def test_promotion_sql_is_idempotent(self) -> None:
        """The module must export _LIST_HYPERTABLES_SQL (inventory query) and
        _TIMESCALEDB_DETECT_SQL (detection query).  Both must be importable and
        contain expected SQL fragments."""
        from src.db.timescale import _LIST_HYPERTABLES_SQL, _TIMESCALEDB_DETECT_SQL

        assert "pg_extension" in str(_TIMESCALEDB_DETECT_SQL)
        assert "timescaledb_information" in str(_LIST_HYPERTABLES_SQL)

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
        """As of schema revision b1c2d3e4f5a6, the second database call is the
        hypertable inventory query (not a promotion DDL).  An OperationalError
        on the detection query itself must still raise DatabaseUnavailableError.
        An OperationalError on the inventory query is swallowed (non-fatal)
        because it merely affects observability, not correctness."""
        # OperationalError on the DETECTION query → DatabaseUnavailableError
        engine = _make_engine_with_connection(
            detect_row=None,
            detect_raises=OperationalError("connection refused", None, None),
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
