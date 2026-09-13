"""
tests/unit/engines/test_gap_recovery.py

Unit tests for src/engines/gap_recovery.py — GapRecoveryEngine.

Coverage:
  - create_gap creates a DataGap with PENDING status and a valid UUID v4 gapId
  - create_gap raises ValueError when gap_end_ms < gap_start_ms
  - create_gap stores gap in the in-memory store
  - create_gap correctly computes durationSec from epoch-ms values
  - get_pending_gaps returns only PENDING gaps
  - get_gaps_by_status filters correctly for each status
  - get_gaps_by_status raises ValueError for unknown status
  - get_gap_by_id returns the correct gap or None
  - all_gaps returns every gap regardless of status
  - recover_gap: PENDING → RECOVERING → stays RECOVERING (stub fetch fails,
    attempts < max)
  - recover_gap: after max_attempts → EXHAUSTED (DataIncident emitted)
  - recover_gap: RECOVERED/EXHAUSTED gaps are returned unchanged
  - recover_gap: increments recoveryAttempts on each call
  - recover_gap: assigns recoveryProvider from Capability_Matrix fallback
  - recover_gap persists updated state to the database
  - load_gaps_from_db loads rows into the in-memory store
  - DataGap.to_dict serialises all fields correctly
  - gapId is a valid UUID v4 string

Requirements: 4.7, 4.8, 10.8, 10.10
"""

from __future__ import annotations

import uuid
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.engines.gap_recovery import (
    DataGap,
    GapRecoveryEngine,
    _make_exhausted_incident,
    _resolve_fallback_provider,
    _ms_to_utc_datetime,
    _STATUS_PENDING,
    _STATUS_RECOVERING,
    _STATUS_RECOVERED,
    _STATUS_EXHAUSTED,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GAP_START_MS = 1_705_300_000_000   # arbitrary fixed epoch-ms
_GAP_END_MS   = 1_705_300_060_000   # 60 s later


def _make_engine(max_attempts: int = 3) -> GapRecoveryEngine:
    return GapRecoveryEngine(max_attempts=max_attempts)


def _make_gap(engine: GapRecoveryEngine, **kwargs: Any) -> DataGap:
    """Create a gap with defaults that can be overridden."""
    defaults = dict(
        instrument_id="NSE:RELIANCE:EQ",
        exchange="NSE",
        interval="1m",
        gap_start_ms=_GAP_START_MS,
        gap_end_ms=_GAP_END_MS,
        expected_provider="angel_one",
    )
    defaults.update(kwargs)
    return engine.create_gap(**defaults)


class _FakeDB:
    """Fake async SQLAlchemy engine that captures executed statements."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def execute(self, stmt: Any, params: Any = None) -> None:
        self.executed.append((stmt, params))


class _FakeRedis:
    """Minimal async Redis fake (no-op for gap recovery tests)."""

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        pass


# ---------------------------------------------------------------------------
# create_gap
# ---------------------------------------------------------------------------


class TestCreateGap:
    def test_creates_gap_with_pending_status(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        assert gap.recoveryStatus == _STATUS_PENDING

    def test_gap_id_is_uuid_v4(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        # Must not raise — confirms it is a valid UUID.
        parsed = uuid.UUID(gap.gapId, version=4)
        assert str(parsed) == gap.gapId

    def test_gap_id_is_unique_across_multiple_gaps(self) -> None:
        engine = _make_engine()
        gaps = [_make_gap(engine) for _ in range(10)]
        ids = {g.gapId for g in gaps}
        assert len(ids) == 10, "All gapIds must be unique"

    def test_gap_stored_in_memory(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        assert engine.get_gap_by_id(gap.gapId) is gap

    def test_gap_fields_set_correctly(self) -> None:
        engine = _make_engine()
        gap = engine.create_gap(
            instrument_id="NFO:NIFTY:FUTIDX",
            exchange="NFO",
            interval="5m",
            gap_start_ms=_GAP_START_MS,
            gap_end_ms=_GAP_END_MS,
            expected_provider="upstox",
        )
        assert gap.instrumentId == "NFO:NIFTY:FUTIDX"
        assert gap.exchange == "NFO"
        assert gap.intervalStr == "5m"
        assert gap.gapStart == _GAP_START_MS
        assert gap.gapEnd == _GAP_END_MS
        assert gap.expectedProvider == "upstox"
        assert gap.recoveryProvider is None
        assert gap.recoveryAttempts == 0

    def test_duration_sec_computed_correctly(self) -> None:
        engine = _make_engine()
        # gap_end - gap_start = 300 000 ms = 300 s
        gap = engine.create_gap(
            instrument_id="NSE:NIFTY:IDX",
            exchange="NSE",
            interval="5m",
            gap_start_ms=1_000_000_000_000,
            gap_end_ms=1_000_000_300_000,
            expected_provider="upstox",
        )
        assert gap.durationSec == 300

    def test_raises_when_end_before_start(self) -> None:
        engine = _make_engine()
        with pytest.raises(ValueError, match="gap_end_ms"):
            engine.create_gap(
                instrument_id="NSE:RELIANCE:EQ",
                exchange="NSE",
                interval="1m",
                gap_start_ms=_GAP_END_MS,   # swapped on purpose
                gap_end_ms=_GAP_START_MS,
                expected_provider="angel_one",
            )

    def test_equal_start_end_produces_zero_duration(self) -> None:
        engine = _make_engine()
        gap = engine.create_gap(
            instrument_id="NSE:RELIANCE:EQ",
            exchange="NSE",
            interval="1m",
            gap_start_ms=_GAP_START_MS,
            gap_end_ms=_GAP_START_MS,
            expected_provider="angel_one",
        )
        assert gap.durationSec == 0


# ---------------------------------------------------------------------------
# get_pending_gaps / get_gaps_by_status
# ---------------------------------------------------------------------------


class TestQueryHelpers:
    def test_get_pending_gaps_returns_only_pending(self) -> None:
        engine = _make_engine()
        pending = _make_gap(engine)
        recovering = _make_gap(engine)
        recovering.recoveryStatus = _STATUS_RECOVERING
        recovered = _make_gap(engine)
        recovered.recoveryStatus = _STATUS_RECOVERED

        result = engine.get_pending_gaps()
        assert result == [pending]

    def test_get_pending_gaps_empty_when_none_pending(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        gap.recoveryStatus = _STATUS_RECOVERED
        assert engine.get_pending_gaps() == []

    def test_get_gaps_by_status_pending(self) -> None:
        engine = _make_engine()
        g1 = _make_gap(engine)
        g2 = _make_gap(engine)
        g2.recoveryStatus = _STATUS_EXHAUSTED
        result = engine.get_gaps_by_status(_STATUS_PENDING)
        assert result == [g1]

    def test_get_gaps_by_status_recovered(self) -> None:
        engine = _make_engine()
        g1 = _make_gap(engine)
        g1.recoveryStatus = _STATUS_RECOVERED
        g2 = _make_gap(engine)  # stays PENDING
        result = engine.get_gaps_by_status(_STATUS_RECOVERED)
        assert result == [g1]

    def test_get_gaps_by_status_raises_for_unknown(self) -> None:
        engine = _make_engine()
        with pytest.raises(ValueError, match="Unknown gap status"):
            engine.get_gaps_by_status("BOGUS_STATUS")

    def test_all_gaps_returns_all(self) -> None:
        engine = _make_engine()
        g1 = _make_gap(engine)
        g2 = _make_gap(engine)
        g2.recoveryStatus = _STATUS_RECOVERED
        all_ids = {g.gapId for g in engine.all_gaps()}
        assert g1.gapId in all_ids
        assert g2.gapId in all_ids
        assert len(engine.all_gaps()) == 2

    def test_get_gap_by_id_returns_none_for_missing(self) -> None:
        engine = _make_engine()
        assert engine.get_gap_by_id("nonexistent-id") is None


# ---------------------------------------------------------------------------
# recover_gap state transitions
# ---------------------------------------------------------------------------


class TestRecoverGap:
    """Tests for the recovery state machine.

    The stub _attempt_fetch always returns False, so real provider calls are
    not made.  We can verify state transitions by controlling max_attempts.
    """

    @pytest.mark.asyncio
    async def test_first_attempt_transitions_to_recovering(self) -> None:
        engine = _make_engine(max_attempts=5)
        gap = _make_gap(engine)
        db = _FakeDB()
        redis = _FakeRedis()

        updated = await engine.recover_gap(gap, db, redis)

        # Not yet exhausted (1 attempt out of 5) — stays RECOVERING.
        assert updated.recoveryStatus == _STATUS_RECOVERING
        assert updated.recoveryAttempts == 1

    @pytest.mark.asyncio
    async def test_increments_recovery_attempts_each_call(self) -> None:
        engine = _make_engine(max_attempts=5)
        gap = _make_gap(engine)
        db = _FakeDB()
        redis = _FakeRedis()

        for expected_attempts in range(1, 4):
            await engine.recover_gap(gap, db, redis)
            assert gap.recoveryAttempts == expected_attempts

    @pytest.mark.asyncio
    async def test_exhausted_after_max_attempts(self) -> None:
        engine = _make_engine(max_attempts=2)
        gap = _make_gap(engine)
        db = _FakeDB()
        redis = _FakeRedis()

        # Attempt 1 — RECOVERING
        await engine.recover_gap(gap, db, redis)
        assert gap.recoveryStatus == _STATUS_RECOVERING
        assert gap.recoveryAttempts == 1

        # Attempt 2 — EXHAUSTED
        await engine.recover_gap(gap, db, redis)
        assert gap.recoveryStatus == _STATUS_EXHAUSTED
        assert gap.recoveryAttempts == 2

    @pytest.mark.asyncio
    async def test_recovered_gap_is_returned_unchanged(self) -> None:
        engine = _make_engine(max_attempts=3)
        gap = _make_gap(engine)
        gap.recoveryStatus = _STATUS_RECOVERED
        gap.recoveryAttempts = 1

        db = _FakeDB()
        redis = _FakeRedis()

        result = await engine.recover_gap(gap, db, redis)
        assert result.recoveryStatus == _STATUS_RECOVERED
        assert result.recoveryAttempts == 1  # unchanged

    @pytest.mark.asyncio
    async def test_exhausted_gap_is_returned_unchanged(self) -> None:
        engine = _make_engine(max_attempts=1)
        gap = _make_gap(engine)
        gap.recoveryStatus = _STATUS_EXHAUSTED
        gap.recoveryAttempts = 1

        db = _FakeDB()
        redis = _FakeRedis()

        result = await engine.recover_gap(gap, db, redis)
        assert result.recoveryStatus == _STATUS_EXHAUSTED
        assert result.recoveryAttempts == 1  # unchanged

    @pytest.mark.asyncio
    async def test_recovery_provider_set_from_fallback(self) -> None:
        engine = _make_engine(max_attempts=5)
        gap = _make_gap(engine, expected_provider="angel_one")
        db = _FakeDB()
        redis = _FakeRedis()

        await engine.recover_gap(gap, db, redis)

        # Angel One's fallback in the Capability_Matrix is Upstox.
        assert gap.recoveryProvider == "upstox"

    @pytest.mark.asyncio
    async def test_persist_gap_called_after_recovery(self) -> None:
        engine = _make_engine(max_attempts=3)
        gap = _make_gap(engine)
        db = _FakeDB()
        redis = _FakeRedis()

        # Patch persist_gap to track calls without actually writing to DB.
        engine.persist_gap = AsyncMock()

        await engine.recover_gap(gap, db, redis)

        engine.persist_gap.assert_called_once_with(gap, db)

    @pytest.mark.asyncio
    async def test_in_memory_store_updated_after_recovery(self) -> None:
        engine = _make_engine(max_attempts=3)
        gap = _make_gap(engine)
        db = _FakeDB()
        redis = _FakeRedis()

        engine.persist_gap = AsyncMock()
        await engine.recover_gap(gap, db, redis)

        stored = engine.get_gap_by_id(gap.gapId)
        assert stored is gap
        assert stored.recoveryStatus == _STATUS_RECOVERING


# ---------------------------------------------------------------------------
# max_attempts clamping
# ---------------------------------------------------------------------------


class TestMaxAttemptsClamping:
    def test_max_attempts_clamped_to_minimum_1(self) -> None:
        engine = GapRecoveryEngine(max_attempts=0)
        assert engine._max_attempts == 1

    def test_max_attempts_clamped_to_maximum_10(self) -> None:
        engine = GapRecoveryEngine(max_attempts=99)
        assert engine._max_attempts == 10

    def test_valid_max_attempts_preserved(self) -> None:
        engine = GapRecoveryEngine(max_attempts=7)
        assert engine._max_attempts == 7


# ---------------------------------------------------------------------------
# load_gaps_from_db
# ---------------------------------------------------------------------------


class TestLoadGapsFromDb:
    @pytest.mark.asyncio
    async def test_loads_rows_into_memory_store(self) -> None:
        engine = _make_engine()

        # Build a fake result row using a simple namespace.
        class _Row:
            gap_id = str(uuid.uuid4())
            instrument_id = "NSE:HDFC:EQ"
            exchange = "NSE"
            interval_str = "5m"
            gap_start_ms = 1_705_300_000_000
            gap_end_ms   = 1_705_300_300_000
            duration_sec = 300
            recovery_status = "PENDING"
            recovery_attempts = 0
            expected_provider = "angel_one"
            recovery_provider = None

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [_Row()]

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = mock_result

        # Context-manager for engine.connect()
        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_db = MagicMock()
        mock_db.connect.return_value = mock_conn_ctx

        count = await engine.load_gaps_from_db(mock_db)

        assert count == 1
        loaded_gap = engine.get_gap_by_id(_Row.gap_id)
        assert loaded_gap is not None
        assert loaded_gap.instrumentId == "NSE:HDFC:EQ"
        assert loaded_gap.recoveryStatus == "PENDING"

    @pytest.mark.asyncio
    async def test_returns_zero_on_db_error(self) -> None:
        engine = _make_engine()

        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_db = MagicMock()
        mock_db.connect.return_value = mock_conn_ctx

        count = await engine.load_gaps_from_db(mock_db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_existing_in_memory_entries_preserved(self) -> None:
        """Gaps created before load_gaps_from_db are not removed."""
        engine = _make_engine()
        pre_existing = _make_gap(engine)

        # DB returns no rows.
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = mock_result

        mock_conn_ctx = AsyncMock()
        mock_conn_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_db = MagicMock()
        mock_db.connect.return_value = mock_conn_ctx

        count = await engine.load_gaps_from_db(mock_db)

        assert count == 0
        assert engine.get_gap_by_id(pre_existing.gapId) is pre_existing


# ---------------------------------------------------------------------------
# DataGap.to_dict serialisation
# ---------------------------------------------------------------------------


class TestDataGapSerialisation:
    def test_to_dict_contains_all_required_fields(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        d = gap.to_dict()

        expected_keys = {
            "gapId", "instrumentId", "exchange", "intervalStr",
            "gapStart", "gapEnd", "durationSec", "recoveryStatus",
            "recoveryAttempts", "expectedProvider", "recoveryProvider",
        }
        assert expected_keys.issubset(d.keys())

    def test_to_dict_values_match_attributes(self) -> None:
        engine = _make_engine()
        gap = _make_gap(
            engine,
            instrument_id="NSE:TCS:EQ",
            exchange="NSE",
            interval="15m",
            gap_start_ms=_GAP_START_MS,
            gap_end_ms=_GAP_END_MS,
            expected_provider="openchart",
        )
        d = gap.to_dict()

        assert d["instrumentId"] == "NSE:TCS:EQ"
        assert d["exchange"] == "NSE"
        assert d["intervalStr"] == "15m"
        assert d["gapStart"] == _GAP_START_MS
        assert d["gapEnd"] == _GAP_END_MS
        assert d["expectedProvider"] == "openchart"
        assert d["recoveryProvider"] is None
        assert d["recoveryStatus"] == _STATUS_PENDING

    def test_to_dict_reflects_updated_status(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        gap.recoveryStatus = _STATUS_EXHAUSTED
        gap.recoveryProvider = "openchart"
        gap.recoveryAttempts = 5

        d = gap.to_dict()
        assert d["recoveryStatus"] == _STATUS_EXHAUSTED
        assert d["recoveryProvider"] == "openchart"
        assert d["recoveryAttempts"] == 5


# ---------------------------------------------------------------------------
# _make_exhausted_incident
# ---------------------------------------------------------------------------


class TestMakeExhaustedIncident:
    def test_incident_has_required_fields(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        gap.recoveryAttempts = 3
        exhausted_at = "2024-01-15T09:00:00.000000+00:00"

        incident = _make_exhausted_incident(gap=gap, exhausted_at=exhausted_at)

        assert "incidentId" in incident
        assert incident["incidentType"] == "GAP_RECOVERY_EXHAUSTED"
        assert incident["instrumentId"] == gap.instrumentId
        assert incident["severity"] == "HIGH"
        assert incident["details"]["symbol"] == "RELIANCE"
        assert incident["details"]["exchange"] == "NSE"
        assert incident["details"]["interval"] == "1m"
        assert incident["details"]["gapStart"] == _GAP_START_MS
        assert incident["details"]["gapEnd"] == _GAP_END_MS
        assert incident["details"]["exhaustedAt"] == exhausted_at
        assert incident["details"]["recoveryAttempts"] == 3

    def test_incident_id_is_uuid_v4(self) -> None:
        engine = _make_engine()
        gap = _make_gap(engine)
        incident = _make_exhausted_incident(
            gap=gap,
            exhausted_at="2024-01-15T09:00:00+00:00",
        )
        parsed = uuid.UUID(incident["incidentId"], version=4)
        assert str(parsed) == incident["incidentId"]


# ---------------------------------------------------------------------------
# _resolve_fallback_provider
# ---------------------------------------------------------------------------


class TestResolveFallbackProvider:
    def test_angel_one_fallback_is_upstox(self) -> None:
        result = _resolve_fallback_provider(
            instrument_id="NSE:RELIANCE:EQ",
            interval="1m",
            expected_provider="angel_one",
        )
        assert result == "upstox"

    def test_upstox_fallback_is_angel_one(self) -> None:
        result = _resolve_fallback_provider(
            instrument_id="NSE:NIFTY:IDX",
            interval="5m",
            expected_provider="upstox",
        )
        assert result == "angel_one"

    def test_jugaad_fallback_is_openchart(self) -> None:
        result = _resolve_fallback_provider(
            instrument_id="NFO:NIFTY:FUTIDX",
            interval="1d",
            expected_provider="jugaad_data",
        )
        assert result == "openchart"

    def test_unknown_provider_defaults_to_openchart(self) -> None:
        result = _resolve_fallback_provider(
            instrument_id="NSE:HDFC:EQ",
            interval="1h",
            expected_provider="some_unknown_provider",
        )
        assert result == "openchart"


# ---------------------------------------------------------------------------
# _ms_to_utc_datetime helper
# ---------------------------------------------------------------------------


class TestMsToUtcDatetime:
    def test_converts_epoch_ms_to_utc_datetime(self) -> None:
        epoch_ms = 1_705_300_000_000
        result = _ms_to_utc_datetime(epoch_ms)
        assert result.tzinfo is not None
        # Verify: epoch_ms / 1000 = 1705300000.0 seconds since Unix epoch.
        expected = datetime.datetime.fromtimestamp(
            epoch_ms / 1000.0, tz=datetime.timezone.utc
        )
        assert result == expected

    def test_result_is_utc_aware(self) -> None:
        result = _ms_to_utc_datetime(0)
        assert result.tzinfo == datetime.timezone.utc
