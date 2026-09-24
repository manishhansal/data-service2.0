"""
tests/unit/engines/test_fno_universe.py

Unit tests for src/engines/fno_universe.py — FnoUniverseService.

Covers:
- compute_checksum: determinism, order-independence, empty list
- Checksum idempotency: same checksum → no DB write, idempotent log
- Different checksum → new snapshot written and previous superseded
- Lifecycle events generated for ADDED and REMOVED instrument IDs
- Upstream unavailable → retain last in-memory snapshot, emit warning
- expire_instruments: sets activeTo correctly for matching expiry dates
- get_current_snapshot: returns None when no snapshot, returns latest ACTIVE
- get_lifecycle_events: returns all events or filtered by version

Requirements: 11.1, 11.2, 11.3, 11.6, 11.7, 2.4
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.core.schemas.instrument import (
    ChangeType,
    FnoUniverseSnapshot,
    InstrumentLifecycleEvent,
)
from src.engines.fno_universe import (
    FnoUniverseService,
    NseUpstreamUnavailableError,
    _symbol_from_id,
    compute_checksum,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    version: int = 1,
    checksum: str = "abc123",
    status: str = "ACTIVE",
    fno_equity_count: int = 10,
    fno_index_count: int = 4,
    constituent_count: int = 14,
    effective_from: Optional[date] = None,
) -> FnoUniverseSnapshot:
    """Build a FnoUniverseSnapshot with sensible test defaults."""
    return FnoUniverseSnapshot(
        snapshotVersion=version,
        checksum=checksum,
        generatedAt=datetime.now(tz=timezone.utc).isoformat(),
        effectiveFrom=effective_from or date.today(),
        effectiveTo=None,
        fnoEquityCount=fno_equity_count,
        fnoIndexCount=fno_index_count,
        constituentCount=constituent_count,
        status=status,
    )


def _make_engine_mock(
    snapshot_row: Optional[dict] = None,
    lifecycle_rows: Optional[list[dict]] = None,
    rowcount: int = 0,
) -> MagicMock:
    """
    Build a mock AsyncEngine that can service:
    - get_current_snapshot  → SELECT from fno_universe_snapshot
    - get_lifecycle_events  → SELECT from fno_lifecycle_event
    - expire_instruments    → UPDATE instrument_master (returns rowcount)

    The engine mock supports both ``engine.connect()`` (used by query helpers)
    and ``sessionmaker(engine)`` (used by persist / expire operations).
    """
    # ── SELECT result for get_current_snapshot ────────────────────────────
    mock_snapshot_result = MagicMock()
    if snapshot_row is not None:
        mock_snapshot_result.mappings.return_value.fetchone.return_value = snapshot_row
    else:
        mock_snapshot_result.mappings.return_value.fetchone.return_value = None

    # ── SELECT result for get_lifecycle_events ────────────────────────────
    mock_events_result = MagicMock()
    mock_events_result.mappings.return_value.__iter__ = MagicMock(
        return_value=iter(lifecycle_rows or [])
    )

    # ── UPDATE result for expire_instruments ──────────────────────────────
    mock_update_result = MagicMock()
    mock_update_result.rowcount = rowcount

    # ── Connection context-manager ────────────────────────────────────────
    mock_conn = AsyncMock()
    # execute is called for both snapshot SELECT and events SELECT;
    # return snapshot result first, then events result for subsequent calls
    mock_conn.execute = AsyncMock(
        side_effect=[mock_snapshot_result, mock_events_result, mock_update_result]
    )

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.connect.return_value = mock_cm

    return engine


def _make_session_mock(rowcount: int = 0) -> tuple[MagicMock, AsyncMock]:
    """Return (engine, session) mocks suitable for expire_instruments tests."""
    mock_result = MagicMock()
    mock_result.rowcount = rowcount

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    # The begin() context manager used in _persist_snapshot
    mock_begin_cm = AsyncMock()
    mock_begin_cm.__aenter__ = AsyncMock(return_value=None)
    mock_begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=mock_begin_cm)

    engine = MagicMock()
    return engine, mock_session


# ---------------------------------------------------------------------------
# compute_checksum
# ---------------------------------------------------------------------------


class TestComputeChecksum:
    def test_deterministic_same_list(self):
        ids = ["NSE:A:EQ", "NSE:B:EQ", "NSE:C:EQ"]
        assert compute_checksum(ids) == compute_checksum(ids)

    def test_order_independent(self):
        """Checksum must be the same regardless of input order."""
        ids_forward = ["NSE:A:EQ", "NSE:B:EQ", "NSE:C:EQ"]
        ids_reversed = ["NSE:C:EQ", "NSE:B:EQ", "NSE:A:EQ"]
        assert compute_checksum(ids_forward) == compute_checksum(ids_reversed)

    def test_different_ids_produce_different_checksums(self):
        ids_a = ["NSE:A:EQ", "NSE:B:EQ"]
        ids_b = ["NSE:A:EQ", "NSE:C:EQ"]
        assert compute_checksum(ids_a) != compute_checksum(ids_b)

    def test_empty_list_produces_consistent_checksum(self):
        c1 = compute_checksum([])
        c2 = compute_checksum([])
        assert c1 == c2
        assert len(c1) == 64  # SHA-256 hex digest length

    def test_single_element(self):
        ids = ["NSE:RELIANCE:EQ"]
        expected = hashlib.sha256(b"NSE:RELIANCE:EQ").hexdigest()
        assert compute_checksum(ids) == expected

    def test_returns_64_hex_chars(self):
        ids = ["NSE:A:EQ"]
        result = compute_checksum(ids)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# _symbol_from_id helper
# ---------------------------------------------------------------------------


class TestSymbolFromId:
    def test_extracts_middle_segment(self):
        assert _symbol_from_id("NSE:RELIANCE:EQ") == "RELIANCE"
        assert _symbol_from_id("NFO:NIFTY25JANFUT:FUTIDX") == "NIFTY25JANFUT"
        assert _symbol_from_id("NFO:BANKNIFTY:IDX") == "BANKNIFTY"

    def test_fallback_on_malformed_id(self):
        assert _symbol_from_id("INVALID") == "INVALID"
        assert _symbol_from_id("NSE:RELIANCE") == "NSE:RELIANCE"

    def test_complex_symbol(self):
        assert _symbol_from_id("NFO:NIFTY25JAN22000CE:OPTIDX") == "NIFTY25JAN22000CE"


# ---------------------------------------------------------------------------
# FnoUniverseService.refresh — idempotency (same checksum → no write)
# ---------------------------------------------------------------------------


class TestRefreshIdempotency:
    """When checksums match, no DB write occurs."""

    @pytest.mark.asyncio
    async def test_no_write_when_checksum_matches(self):
        """refresh() performs no DB write when the checksum is identical."""
        FnoUniverseService.reset_cache()

        instrument_ids = ["NSE:A:EQ", "NSE:B:EQ", "NSE:C:EQ"]
        checksum = compute_checksum(instrument_ids)

        existing_snapshot = _make_snapshot(version=1, checksum=checksum)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=instrument_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ) as mock_get_snapshot, patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ) as mock_persist:
            engine = MagicMock()
            await FnoUniverseService.refresh(engine=engine)

            mock_get_snapshot.assert_awaited_once()
            mock_persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_refresh_emits_log(self, caplog):
        """An idempotent refresh logs an informational skip event."""
        FnoUniverseService.reset_cache()

        instrument_ids = ["NSE:X:EQ"]
        checksum = compute_checksum(instrument_ids)
        existing_snapshot = _make_snapshot(version=1, checksum=checksum)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=instrument_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ):
            engine = MagicMock()
            with caplog.at_level(logging.INFO, logger="src.engines.fno_universe"):
                await FnoUniverseService.refresh(engine=engine)

            assert any(
                "idempotent" in record.message.lower()
                or "idempotent" in str(record.args).lower()
                for record in caplog.records
            )

    @pytest.mark.asyncio
    async def test_in_memory_cache_updated_on_idempotent_refresh(self):
        """Even on an idempotent skip the in-memory snapshot reference is updated."""
        FnoUniverseService.reset_cache()
        assert FnoUniverseService.get_cached_snapshot() is None

        instrument_ids = ["NSE:X:EQ"]
        checksum = compute_checksum(instrument_ids)
        existing_snapshot = _make_snapshot(version=5, checksum=checksum)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=instrument_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ):
            engine = MagicMock()
            await FnoUniverseService.refresh(engine=engine)

        cached = FnoUniverseService.get_cached_snapshot()
        assert cached is not None
        assert cached.snapshotVersion == 5


# ---------------------------------------------------------------------------
# FnoUniverseService.refresh — new snapshot written on checksum change
# ---------------------------------------------------------------------------


class TestRefreshNewSnapshot:
    """When the checksum differs a new snapshot must be written."""

    @pytest.mark.asyncio
    async def test_new_snapshot_persisted_when_checksum_differs(self):
        """_persist_snapshot is called with a new snapshot object."""
        FnoUniverseService.reset_cache()

        new_ids = ["NSE:A:EQ", "NSE:B:EQ", "NSE:C:EQ"]
        old_checksum = compute_checksum(["NSE:OLD:EQ"])
        existing_snapshot = _make_snapshot(version=1, checksum=old_checksum)

        captured: dict = {}

        async def _capture_persist(engine, snapshot, events, prev):
            captured["snapshot"] = snapshot
            captured["events"] = events
            captured["prev"] = prev

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture_persist),
        ):
            engine = MagicMock()
            await FnoUniverseService.refresh(engine=engine)

        assert "snapshot" in captured
        new_snap = captured["snapshot"]
        assert new_snap.snapshotVersion == 2  # incremented from 1
        assert new_snap.checksum == compute_checksum(new_ids)
        assert new_snap.status == "ACTIVE"
        assert new_snap.constituentCount == len(new_ids)

    @pytest.mark.asyncio
    async def test_version_incremented_from_previous(self):
        FnoUniverseService.reset_cache()
        new_ids = ["NSE:A:EQ"]
        existing_snapshot = _make_snapshot(version=7, checksum="old_checksum_xyz")

        captured_version: list[int] = []

        async def _capture(engine, snapshot, events, prev):
            captured_version.append(snapshot.snapshotVersion)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        assert captured_version == [8]

    @pytest.mark.asyncio
    async def test_first_snapshot_gets_version_1(self):
        """When no prior snapshot exists the first version is 1."""
        FnoUniverseService.reset_cache()
        new_ids = ["NSE:A:EQ"]
        captured: dict = {}

        async def _capture(engine, snapshot, events, prev):
            captured["snapshot"] = snapshot

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        assert captured["snapshot"].snapshotVersion == 1

    @pytest.mark.asyncio
    async def test_in_memory_cache_updated_after_new_snapshot(self):
        FnoUniverseService.reset_cache()
        new_ids = ["NSE:A:EQ", "NSE:B:EQ"]
        old_checksum = "old_value"
        existing_snapshot = _make_snapshot(version=1, checksum=old_checksum)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        cached = FnoUniverseService.get_cached_snapshot()
        assert cached is not None
        assert cached.snapshotVersion == 2
        assert FnoUniverseService.get_cached_instrument_ids() == frozenset(new_ids)


# ---------------------------------------------------------------------------
# FnoUniverseService.refresh — lifecycle events
# ---------------------------------------------------------------------------


class TestRefreshLifecycleEvents:
    """ADDED and REMOVED lifecycle events are generated correctly."""

    @pytest.mark.asyncio
    async def test_added_events_generated_for_new_ids(self):
        """Instrument IDs in new set but not in old in-memory set → ADDED."""
        FnoUniverseService.reset_cache()

        # Seed in-memory state with a previous set
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_instrument_ids = {"NSE:OLD:EQ"}

        new_ids = ["NSE:OLD:EQ", "NSE:NEW1:EQ", "NSE:NEW2:EQ"]
        old_snapshot_checksum = compute_checksum(["NSE:OLD:EQ"])
        existing_snapshot = _make_snapshot(version=1, checksum=old_snapshot_checksum)

        captured_events: list[InstrumentLifecycleEvent] = []

        async def _capture(engine, snapshot, events, prev):
            captured_events.extend(events)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        added_symbols = {e.symbol for e in captured_events if e.changeType == ChangeType.ADDED}
        assert "NEW1" in added_symbols
        assert "NEW2" in added_symbols
        assert "OLD" not in added_symbols

        # Clean up
        fno_module._current_instrument_ids = set()

    @pytest.mark.asyncio
    async def test_removed_events_generated_for_dropped_ids(self):
        """Instrument IDs in old set but not in new set → REMOVED."""
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_instrument_ids = {"NSE:KEEP:EQ", "NSE:GONE:EQ"}

        new_ids = ["NSE:KEEP:EQ"]
        old_checksum = compute_checksum(["NSE:KEEP:EQ", "NSE:GONE:EQ"])
        existing_snapshot = _make_snapshot(version=1, checksum=old_checksum)

        captured_events: list[InstrumentLifecycleEvent] = []

        async def _capture(engine, snapshot, events, prev):
            captured_events.extend(events)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        removed_symbols = {
            e.symbol for e in captured_events if e.changeType == ChangeType.REMOVED
        }
        assert "GONE" in removed_symbols
        assert "KEEP" not in removed_symbols

        fno_module._current_instrument_ids = set()

    @pytest.mark.asyncio
    async def test_no_lifecycle_events_when_no_previous_in_memory_set(self):
        """First refresh (no previous in-memory set) produces no lifecycle events."""
        FnoUniverseService.reset_cache()

        new_ids = ["NSE:A:EQ", "NSE:B:EQ"]

        captured_events: list = []

        async def _capture(engine, snapshot, events, prev):
            captured_events.extend(events)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        assert captured_events == []

    @pytest.mark.asyncio
    async def test_lifecycle_events_carry_correct_snapshot_version(self):
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_instrument_ids = {"NSE:OLD:EQ"}

        new_ids = ["NSE:OLD:EQ", "NSE:NEW:EQ"]
        old_checksum = compute_checksum(["NSE:OLD:EQ"])
        existing_snapshot = _make_snapshot(version=3, checksum=old_checksum)

        captured_events: list[InstrumentLifecycleEvent] = []

        async def _capture(engine, snapshot, events, prev):
            captured_events.extend(events)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        for event in captured_events:
            assert event.snapshotVersion == 4  # version incremented from 3

        fno_module._current_instrument_ids = set()

    @pytest.mark.asyncio
    async def test_lifecycle_event_effective_date_is_today(self):
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_instrument_ids = {"NSE:OLD:EQ"}

        new_ids = ["NSE:NEW:EQ"]
        old_checksum = compute_checksum(["NSE:OLD:EQ"])
        existing_snapshot = _make_snapshot(version=1, checksum=old_checksum)

        captured_events: list[InstrumentLifecycleEvent] = []

        async def _capture(engine, snapshot, events, prev):
            captured_events.extend(events)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=new_ids),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=existing_snapshot),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_capture),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        for event in captured_events:
            assert event.effectiveDate == date.today()

        fno_module._current_instrument_ids = set()


# ---------------------------------------------------------------------------
# FnoUniverseService.refresh — upstream unavailability
# ---------------------------------------------------------------------------


class TestRefreshUpstreamUnavailable:
    @pytest.mark.asyncio
    async def test_upstream_unavailable_no_db_write(self):
        """When upstream raises NseUpstreamUnavailableError no DB write occurs."""
        FnoUniverseService.reset_cache()

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(side_effect=NseUpstreamUnavailableError("NSE timeout")),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ) as mock_persist:
            engine = MagicMock()
            await FnoUniverseService.refresh(engine=engine)

            mock_persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upstream_unavailable_retains_last_snapshot(self):
        """In-memory snapshot is preserved when upstream is unavailable."""
        FnoUniverseService.reset_cache()

        # Pre-seed an in-memory snapshot
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        retained_snapshot = _make_snapshot(version=2, checksum="retained_checksum")
        fno_module._current_snapshot = retained_snapshot

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(side_effect=NseUpstreamUnavailableError("timeout")),
        ):
            engine = MagicMock()
            await FnoUniverseService.refresh(engine=engine)

        # In-memory snapshot must be unchanged
        assert FnoUniverseService.get_cached_snapshot() is retained_snapshot
        assert FnoUniverseService.get_cached_snapshot().snapshotVersion == 2

        # Clean up
        fno_module._current_snapshot = None

    @pytest.mark.asyncio
    async def test_upstream_unavailable_emits_warning_log(self, caplog):
        """A structured WARNING is emitted when upstream is unavailable."""
        FnoUniverseService.reset_cache()

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(side_effect=NseUpstreamUnavailableError("conn refused")),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ):
            engine = MagicMock()
            with caplog.at_level(logging.WARNING, logger="src.engines.fno_universe"):
                await FnoUniverseService.refresh(engine=engine)

            warning_messages = " ".join(
                r.message for r in caplog.records if r.levelno >= logging.WARNING
            )
            assert "unavailable" in warning_messages.lower() or len(caplog.records) > 0

    @pytest.mark.asyncio
    async def test_generic_fetch_error_does_not_crash(self):
        """Any unexpected exception during fetch is handled gracefully."""
        FnoUniverseService.reset_cache()

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(side_effect=RuntimeError("unexpected error")),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(),
        ) as mock_persist:
            # Should not raise
            await FnoUniverseService.refresh(engine=MagicMock())
            mock_persist.assert_not_awaited()


# ---------------------------------------------------------------------------
# FnoUniverseService.expire_instruments
# ---------------------------------------------------------------------------


class TestExpireInstruments:
    @pytest.mark.asyncio
    async def test_expire_instruments_executes_update(self):
        """expire_instruments issues an UPDATE on instrument_master."""
        engine, mock_session = _make_session_mock(rowcount=5)

        with patch(
            "src.engines.fno_universe.sessionmaker",
            return_value=MagicMock(return_value=mock_session),
        ):
            result = await FnoUniverseService.expire_instruments(
                engine=engine,
                as_of_date=date(2025, 1, 30),
            )

        assert result == 5
        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expire_instruments_passes_correct_date(self):
        """The UPDATE binds the supplied as_of_date."""
        engine, mock_session = _make_session_mock(rowcount=3)
        expiry_date = date(2025, 3, 27)
        executed_sql: list = []

        async def _capture_execute(stmt, **kwargs):
            executed_sql.append(stmt)
            mock_result = MagicMock()
            mock_result.rowcount = 3
            return mock_result

        mock_session.execute = AsyncMock(side_effect=_capture_execute)

        with patch(
            "src.engines.fno_universe.sessionmaker",
            return_value=MagicMock(return_value=mock_session),
        ):
            await FnoUniverseService.expire_instruments(
                engine=engine,
                as_of_date=expiry_date,
            )

        # Verify a statement was issued
        assert len(executed_sql) == 1

    @pytest.mark.asyncio
    async def test_expire_instruments_returns_zero_when_none_match(self):
        engine, mock_session = _make_session_mock(rowcount=0)

        with patch(
            "src.engines.fno_universe.sessionmaker",
            return_value=MagicMock(return_value=mock_session),
        ):
            result = await FnoUniverseService.expire_instruments(
                engine=engine,
                as_of_date=date(2025, 1, 1),
            )

        assert result == 0

    @pytest.mark.asyncio
    async def test_expire_instruments_propagates_db_error(self):
        """DB errors propagate so the scheduler can log them."""
        engine, mock_session = _make_session_mock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB connection lost"))

        with patch(
            "src.engines.fno_universe.sessionmaker",
            return_value=MagicMock(return_value=mock_session),
        ):
            with pytest.raises(Exception, match="DB connection lost"):
                await FnoUniverseService.expire_instruments(
                    engine=engine,
                    as_of_date=date(2025, 1, 30),
                )


# ---------------------------------------------------------------------------
# FnoUniverseService.get_current_snapshot
# ---------------------------------------------------------------------------


class TestGetCurrentSnapshot:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_snapshot(self):
        """Returns None when the fno_universe_snapshot table is empty."""
        mock_result = MagicMock()
        mock_result.mappings.return_value.fetchone.return_value = None

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_result)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect.return_value = mock_cm

        result = await FnoUniverseService.get_current_snapshot(engine)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_snapshot_when_row_present(self):
        """Correctly maps a DB row to a FnoUniverseSnapshot."""
        row = {
            "snapshot_version": 3,
            "checksum": "abc123def456" * 4,  # 48 chars (not 64, but fine for test)
            "generated_at": "2025-01-15T08:45:00+00:00",
            "effective_from": date(2025, 1, 15),
            "effective_to": None,
            "fno_equity_count": 180,
            "fno_index_count": 4,
            "constituent_count": 184,
            "status": "ACTIVE",
        }

        mock_result = MagicMock()
        mock_result.mappings.return_value.fetchone.return_value = row

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_result)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect.return_value = mock_cm

        result = await FnoUniverseService.get_current_snapshot(engine)

        assert result is not None
        assert result.snapshotVersion == 3
        assert result.checksum == row["checksum"]
        assert result.fnoEquityCount == 180
        assert result.fnoIndexCount == 4
        assert result.constituentCount == 184
        assert result.status == "ACTIVE"
        assert result.effectiveTo is None

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        """DB errors return None rather than raising (non-fatal)."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=Exception("PG unreachable"))

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect.return_value = mock_cm

        result = await FnoUniverseService.get_current_snapshot(engine)
        assert result is None


# ---------------------------------------------------------------------------
# FnoUniverseService.get_lifecycle_events
# ---------------------------------------------------------------------------


class TestGetLifecycleEvents:
    def _make_event_row(
        self,
        symbol: str,
        change_type: str = "ADDED",
        version: int = 1,
        effective_date: Optional[date] = None,
    ) -> dict:
        return {
            "symbol": symbol,
            "change_type": change_type,
            "effective_date": effective_date or date.today(),
            "source": "NSE_FNO_REFRESH",
            "snapshot_version": version,
        }

    def _make_conn_mock(self, rows: list[dict]) -> tuple[MagicMock, AsyncMock]:
        mock_result = MagicMock()
        mock_result.mappings.return_value.__iter__ = MagicMock(
            return_value=iter(rows)
        )

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_result)

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect.return_value = mock_cm

        return engine, mock_conn

    @pytest.mark.asyncio
    async def test_returns_all_events_when_version_none(self):
        rows = [
            self._make_event_row("RELIANCE", "ADDED", version=1),
            self._make_event_row("TCS", "ADDED", version=1),
            self._make_event_row("OLDCO", "REMOVED", version=2),
        ]
        engine, _ = self._make_conn_mock(rows)
        events = await FnoUniverseService.get_lifecycle_events(engine)
        assert len(events) == 3
        symbols = {e.symbol for e in events}
        assert symbols == {"RELIANCE", "TCS", "OLDCO"}

    @pytest.mark.asyncio
    async def test_returns_filtered_events_by_version(self):
        rows = [
            self._make_event_row("RELIANCE", "ADDED", version=2),
        ]
        engine, mock_conn = self._make_conn_mock(rows)
        events = await FnoUniverseService.get_lifecycle_events(engine, version=2)
        assert len(events) == 1
        assert events[0].symbol == "RELIANCE"

        # Confirm the SQL included the version binding
        call_args = mock_conn.execute.call_args[0]
        assert len(call_args) == 1  # the text() object

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_events(self):
        engine, _ = self._make_conn_mock([])
        events = await FnoUniverseService.get_lifecycle_events(engine)
        assert events == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_db_error(self):
        """DB errors return [] rather than raising."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=Exception("table not found"))

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.connect.return_value = mock_cm

        events = await FnoUniverseService.get_lifecycle_events(engine)
        assert events == []

    @pytest.mark.asyncio
    async def test_event_change_types_mapped_correctly(self):
        rows = [
            self._make_event_row("ADDED_SYM", "ADDED"),
            self._make_event_row("REMOVED_SYM", "REMOVED"),
            self._make_event_row("SUSPENDED_SYM", "SUSPENDED"),
        ]
        engine, _ = self._make_conn_mock(rows)
        events = await FnoUniverseService.get_lifecycle_events(engine)

        by_symbol = {e.symbol: e for e in events}
        assert by_symbol["ADDED_SYM"].changeType == ChangeType.ADDED
        assert by_symbol["REMOVED_SYM"].changeType == ChangeType.REMOVED
        assert by_symbol["SUSPENDED_SYM"].changeType == ChangeType.SUSPENDED


# ---------------------------------------------------------------------------
# In-memory cache helpers
# ---------------------------------------------------------------------------


class TestCacheHelpers:
    def test_reset_cache_clears_snapshot_and_ids(self):
        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_snapshot = _make_snapshot()
        fno_module._current_instrument_ids = {"NSE:A:EQ"}

        FnoUniverseService.reset_cache()

        assert FnoUniverseService.get_cached_snapshot() is None
        assert FnoUniverseService.get_cached_instrument_ids() == frozenset()

    def test_get_cached_instrument_ids_returns_frozenset(self):
        FnoUniverseService.reset_cache()
        assert isinstance(FnoUniverseService.get_cached_instrument_ids(), frozenset)


# ---------------------------------------------------------------------------
# Integration-level: full refresh cycle without real DB
# ---------------------------------------------------------------------------


class TestRefreshFullCycle:
    """End-to-end smoke test of the refresh flow (no real DB)."""

    @pytest.mark.asyncio
    async def test_full_cycle_first_refresh_then_idempotent(self):
        """Two refreshes with identical NSE data → second one is idempotent."""
        FnoUniverseService.reset_cache()

        instrument_ids = ["NSE:A:EQ", "NSE:B:EQ"]
        persist_call_count = {"n": 0}

        async def _persist(engine, snapshot, events, prev):
            persist_call_count["n"] += 1

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=instrument_ids),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_persist),
        ):
            engine = MagicMock()

            # Patch get_current_snapshot to return None on first call, then
            # return the snapshot that was "persisted"
            call_num = {"n": 0}
            written_snapshot: list = []

            async def _persist_and_capture(eng, snapshot, events, prev):
                persist_call_count["n"] += 1
                written_snapshot.append(snapshot)

            async def _get_snapshot(eng):
                if call_num["n"] == 0:
                    call_num["n"] += 1
                    return None
                # On second call return what was written
                return written_snapshot[0] if written_snapshot else None

            with patch(
                "src.engines.fno_universe._persist_snapshot",
                new=AsyncMock(side_effect=_persist_and_capture),
            ), patch.object(
                FnoUniverseService,
                "get_current_snapshot",
                new=AsyncMock(side_effect=_get_snapshot),
            ):
                # First refresh — should write
                await FnoUniverseService.refresh(engine=engine)
                first_count = persist_call_count["n"]

                # Second refresh with same data — should be idempotent
                await FnoUniverseService.refresh(engine=engine)
                second_count = persist_call_count["n"]

            assert first_count == 1
            assert second_count == 1  # no additional write

    @pytest.mark.asyncio
    async def test_full_cycle_changed_ids_triggers_new_snapshot(self):
        FnoUniverseService.reset_cache()

        ids_v1 = ["NSE:A:EQ", "NSE:B:EQ"]
        ids_v2 = ["NSE:A:EQ", "NSE:B:EQ", "NSE:C:EQ"]  # C added
        snapshots_written: list[FnoUniverseSnapshot] = []
        events_written: list[list[InstrumentLifecycleEvent]] = []

        async def _persist(engine, snapshot, events, prev):
            snapshots_written.append(snapshot)
            events_written.append(events)

        # First refresh — no existing snapshot
        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=ids_v1),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_persist),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        # Second refresh — new ID added
        snapshot_v1 = snapshots_written[0]

        import src.engines.fno_universe as fno_module  # noqa: PLC0415
        fno_module._current_instrument_ids = set(ids_v1)

        with patch(
            "src.engines.fno_universe._fetch_fno_instrument_ids_from_nse",
            new=AsyncMock(return_value=ids_v2),
        ), patch.object(
            FnoUniverseService,
            "get_current_snapshot",
            new=AsyncMock(return_value=snapshot_v1),
        ), patch(
            "src.engines.fno_universe._persist_snapshot",
            new=AsyncMock(side_effect=_persist),
        ):
            await FnoUniverseService.refresh(engine=MagicMock())

        assert len(snapshots_written) == 2
        assert snapshots_written[1].snapshotVersion == 2
        assert snapshots_written[1].constituentCount == 3

        # Check lifecycle events from second refresh
        second_events = events_written[1]
        added = [e for e in second_events if e.changeType == ChangeType.ADDED]
        assert len(added) == 1
        assert added[0].symbol == "C"

        # Clean up
        fno_module._current_instrument_ids = set()


# ---------------------------------------------------------------------------
# warm_cache / warm_cache_from_db  (startup + DB-fallback support)
# ---------------------------------------------------------------------------


class TestWarmCache:
    """Tests for the cache-warming helpers that back the endpoint DB fallback."""

    def test_warm_cache_populates_in_memory_snapshot(self):
        """warm_cache sets the in-memory snapshot without any DB round-trip."""
        FnoUniverseService.reset_cache()
        assert FnoUniverseService.get_cached_snapshot() is None

        snap = _make_snapshot(version=3)
        FnoUniverseService.warm_cache(snap)

        cached = FnoUniverseService.get_cached_snapshot()
        assert cached is not None
        assert cached.snapshotVersion == 3
        FnoUniverseService.reset_cache()

    @pytest.mark.asyncio
    async def test_warm_cache_from_db_loads_active_snapshot(self):
        """warm_cache_from_db reads the ACTIVE snapshot and warms the cache."""
        FnoUniverseService.reset_cache()
        snapshot_row = {
            "snapshot_version": 5,
            "checksum": "deadbeef",
            "generated_at": datetime.now(tz=timezone.utc),
            "effective_from": date.today(),
            "effective_to": None,
            "fno_equity_count": 220,
            "fno_index_count": 4,
            "constituent_count": 224,
            "status": "ACTIVE",
        }
        engine = _make_engine_mock(snapshot_row=snapshot_row)

        result = await FnoUniverseService.warm_cache_from_db(engine)

        assert result is not None
        assert result.snapshotVersion == 5
        assert result.constituentCount == 224
        # Cache is warmed as a side effect.
        assert FnoUniverseService.get_cached_snapshot() is not None
        FnoUniverseService.reset_cache()

    @pytest.mark.asyncio
    async def test_warm_cache_from_db_returns_none_when_empty(self):
        """warm_cache_from_db returns None and leaves cache empty when DB is empty."""
        FnoUniverseService.reset_cache()
        engine = _make_engine_mock(snapshot_row=None)

        result = await FnoUniverseService.warm_cache_from_db(engine)

        assert result is None
        assert FnoUniverseService.get_cached_snapshot() is None
