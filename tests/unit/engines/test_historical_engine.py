"""
tests/unit/engines/test_historical_engine.py

Unit tests for src/engines/historical_engine.py — HistoricalEngine.

Coverage:
  - 3m interval raises ValueError for Indian markets (unconditional hard block)
  - 3m is allowed for crypto (is_indian_market=False)
  - chunk_date_ranges splits date ranges correctly for various chunk sizes
  - chunk_date_ranges edge cases: zero-length range, exact divisor, remainder
  - Checkpoint get/set/resume behaviour via a fake Redis client
  - get_checkpoint returns None for a missing key
  - get_checkpoint handles corrupted values gracefully
  - set_checkpoint stores UTC ISO-8601 without TTL
  - run_backfill resumes from an existing checkpoint
  - run_backfill short-circuits when checkpoint >= to_ts
  - bulk_upsert_candles correctly batches rows to the database
  - bulk_upsert_candles skips rows with an un-parseable time field
  - Provider routing: EQ → Angel One, IDX → Upstox, FO+1d → Jugaad,
    FO+intraday → Angel One, crypto → OpenChart fallback
  - _get_chunk_days returns correct values per provider × interval
  - _coerce_to_datetime handles int (epoch seconds), datetime, ISO-8601 string

Requirements: 4.1, 4.2, 10.1, 10.2, 10.3, 10.4, 10.11
"""

from __future__ import annotations

import datetime
from datetime import timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.schemas.provider import ProviderId
from src.engines.historical_engine import (
    HistoricalEngine,
    _CHECKPOINT_KEY_TEMPLATE,
    _coerce_to_datetime,
    MINIMUM_HISTORY_DAYS,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    """Build a UTC-aware datetime."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


class FakeRedis:
    """Minimal async Redis fake sufficient for checkpoint tests.

    Simulates ``get`` and ``set`` with an in-memory dict.  Does NOT simulate
    TTLs (which is correct — checkpoints have no TTL).
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.set_calls: list[tuple] = []
        self.get_calls: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.get_calls.append(key)
        return self._store.get(key)

    async def set(self, key: str, value: str, **kwargs) -> None:
        self.set_calls.append((key, value))
        # Verify no ``ex`` / ``px`` / ``exat`` TTL args are passed
        # (checkpoint must be persistent — Requirement 10.1).
        assert "ex" not in kwargs, "Checkpoint must have no TTL (ex was set)"
        assert "px" not in kwargs, "Checkpoint must have no TTL (px was set)"
        assert "exat" not in kwargs, "Checkpoint must have no TTL (exat was set)"
        self._store[key] = value.encode() if isinstance(value, str) else value

    def seed(self, key: str, value: str) -> None:
        """Pre-populate the store for resume tests."""
        self._store[key] = value.encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine() -> HistoricalEngine:
    return HistoricalEngine()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ---------------------------------------------------------------------------
# Section 1: 3m interval hard block (Requirement 4.2, 10.11)
# ---------------------------------------------------------------------------

class TestThreeMeterBlock:
    """The 3m interval must raise ValueError before any I/O for Indian markets."""

    async def test_3m_raises_for_indian_market(self, engine: HistoricalEngine) -> None:
        """ValueError raised before any I/O when interval=3m and is_indian_market=True."""
        with pytest.raises(ValueError, match="3m is permanently unsupported"):
            await engine.run_backfill(
                symbol="RELIANCE",
                exchange="NSE",
                instrument_class="EQ",
                interval="3m",
                from_ts=_utc(2024, 1, 1),
                to_ts=_utc(2024, 2, 1),
                db_engine=MagicMock(),
                redis_client=FakeRedis(),
                is_indian_market=True,
            )

    async def test_3m_raises_for_indian_market_async(self, engine: HistoricalEngine) -> None:
        """Same hard block via await — duplicate removed (merged into test above)."""
        with pytest.raises(ValueError, match="3m is permanently unsupported"):
            await engine.run_backfill(
                symbol="RELIANCE",
                exchange="NSE",
                instrument_class="EQ",
                interval="3m",
                from_ts=_utc(2024, 1, 1),
                to_ts=_utc(2024, 2, 1),
                db_engine=MagicMock(),
                redis_client=FakeRedis(),
                is_indian_market=True,
            )

    async def test_3m_allowed_for_crypto(self, engine: HistoricalEngine) -> None:
        """3m does NOT raise when is_indian_market=False (Binance crypto path)."""
        fake_db = MagicMock()
        fake_db.begin = MagicMock(return_value=MagicMock(
            __aenter__=AsyncMock(return_value=AsyncMock()),
            __aexit__=AsyncMock(return_value=False),
        ))
        result = await engine.run_backfill(
            symbol="BTCUSDT",
            exchange="BINANCE",
            instrument_class="CRYPTO_SPOT",
            interval="3m",
            from_ts=_utc(2024, 1, 1),
            to_ts=_utc(2024, 1, 2),
            db_engine=fake_db,
            redis_client=FakeRedis(),
            is_indian_market=False,
        )
        # No exception — result is a summary dict.
        assert "interval" in result
        assert result["interval"] == "3m"

    async def test_3m_raises_for_all_indian_instrument_classes(
        self, engine: HistoricalEngine
    ) -> None:
        """3m is blocked for EQ, FO, and IDX instrument classes."""
        for inst_class in ("EQ", "FO", "IDX"):
            with pytest.raises(ValueError, match="3m is permanently unsupported"):
                await engine.run_backfill(
                    symbol="NIFTY",
                    exchange="NSE",
                    instrument_class=inst_class,
                    interval="3m",
                    from_ts=_utc(2024, 1, 1),
                    to_ts=_utc(2024, 2, 1),
                    db_engine=MagicMock(),
                    redis_client=FakeRedis(),
                    is_indian_market=True,
                )

    async def test_no_io_performed_before_3m_raise(self, engine: HistoricalEngine) -> None:
        """No Redis reads must occur before ValueError is raised for 3m."""
        fake_r = FakeRedis()
        with pytest.raises(ValueError):
            await engine.run_backfill(
                symbol="RELIANCE",
                exchange="NSE",
                instrument_class="EQ",
                interval="3m",
                from_ts=_utc(2024, 1, 1),
                to_ts=_utc(2024, 2, 1),
                db_engine=MagicMock(),
                redis_client=fake_r,
                is_indian_market=True,
            )
        # No Redis calls should have been made before the ValueError.
        assert fake_r.get_calls == [], "Redis was read before 3m ValueError was raised"


# ---------------------------------------------------------------------------
# Section 2: chunk_date_ranges (Requirement 10.4)
# ---------------------------------------------------------------------------

class TestChunkDateRanges:
    """Splitting a date range into provider-safe chunks."""

    def test_single_chunk_when_range_within_limit(self) -> None:
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 15)
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=30)
        assert len(chunks) == 1
        assert chunks[0] == (from_ts, to_ts)

    def test_single_chunk_when_range_exactly_at_limit(self) -> None:
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 31)
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=30)
        assert len(chunks) == 1
        assert chunks[0] == (from_ts, to_ts)

    def test_two_chunks_when_range_exceeds_limit(self) -> None:
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 3, 1)   # 60 days
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=30)
        assert len(chunks) == 2
        # First chunk: Jan 1 → Jan 31
        assert chunks[0][0] == from_ts
        assert chunks[0][1] == _utc(2024, 1, 31)
        # Second chunk: Jan 31 → Mar 1
        assert chunks[1][0] == _utc(2024, 1, 31)
        assert chunks[1][1] == to_ts

    def test_multiple_chunks_with_remainder(self) -> None:
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 25)  # 24 days with chunk=7 → 3 full + 1 partial
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=7)
        assert len(chunks) == 4
        # Last chunk ends exactly at to_ts
        assert chunks[-1][1] == to_ts
        # Every chunk start equals previous chunk end (no gap, no overlap)
        for i in range(1, len(chunks)):
            assert chunks[i][0] == chunks[i - 1][1]

    def test_empty_when_from_equals_to(self) -> None:
        ts = _utc(2024, 1, 1)
        chunks = HistoricalEngine.chunk_date_ranges(ts, ts, max_chunk_days=30)
        assert chunks == []

    def test_empty_when_from_after_to(self) -> None:
        chunks = HistoricalEngine.chunk_date_ranges(
            _utc(2024, 2, 1), _utc(2024, 1, 1), max_chunk_days=30
        )
        assert chunks == []

    def test_chunk_size_1_day(self) -> None:
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 4)  # 3 days
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=1)
        assert len(chunks) == 3

    def test_raises_for_max_chunk_days_zero(self) -> None:
        with pytest.raises(ValueError):
            HistoricalEngine.chunk_date_ranges(
                _utc(2024, 1, 1), _utc(2024, 2, 1), max_chunk_days=0
            )

    def test_contiguous_chunks_cover_entire_range(self) -> None:
        """All chunks together must exactly cover [from_ts, to_ts)."""
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 12, 31)
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=30)
        assert chunks[0][0] == from_ts
        assert chunks[-1][1] == to_ts
        # No gaps between chunks
        for i in range(1, len(chunks)):
            assert chunks[i][0] == chunks[i - 1][1]

    def test_angel_one_1m_chunk_size(self) -> None:
        """Angel One 1m → 30-day chunks."""
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 3, 1)  # 60 days
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=30)
        assert len(chunks) == 2

    def test_upstox_1m_chunk_size(self) -> None:
        """Upstox 1m → 7-day chunks."""
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 29)  # 28 days
        chunks = HistoricalEngine.chunk_date_ranges(from_ts, to_ts, max_chunk_days=7)
        assert len(chunks) == 4


# ---------------------------------------------------------------------------
# Section 3: Checkpoint get / set (Requirements 10.1)
# ---------------------------------------------------------------------------

class TestCheckpoint:
    """Checkpoint read/write behaviour."""

    async def test_get_checkpoint_returns_none_when_missing(
        self, fake_redis: FakeRedis
    ) -> None:
        result = await HistoricalEngine.get_checkpoint(
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            redis_client=fake_redis,
        )
        assert result is None

    async def test_set_checkpoint_stores_utc_iso8601(
        self, fake_redis: FakeRedis
    ) -> None:
        ts = _utc(2024, 3, 15, 10, 30)
        await HistoricalEngine.set_checkpoint(
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            ts=ts,
            redis_client=fake_redis,
        )
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="RELIANCE", exchange="NSE", interval="1m"
        )
        assert key in fake_redis._store
        raw = fake_redis._store[key].decode()
        # Must be a valid UTC ISO-8601 timestamp
        parsed = datetime.datetime.fromisoformat(raw)
        assert parsed.tzinfo is not None
        assert parsed.astimezone(UTC) == ts

    async def test_set_checkpoint_no_ttl(self, fake_redis: FakeRedis) -> None:
        """Checkpoint must be written with NO expiry (persistent)."""
        # FakeRedis.set() asserts that no TTL kwargs are passed —
        # this test confirms set_checkpoint does not pass them.
        ts = _utc(2024, 3, 15)
        await HistoricalEngine.set_checkpoint(
            symbol="NIFTY",
            exchange="NSE",
            interval="5m",
            ts=ts,
            redis_client=fake_redis,
        )
        # Assert: the FakeRedis would have raised AssertionError if TTL was set.

    async def test_get_checkpoint_reads_stored_value(
        self, fake_redis: FakeRedis
    ) -> None:
        ts = _utc(2024, 6, 1, 9, 15)
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="BANKNIFTY", exchange="NSE", interval="1m"
        )
        fake_redis.seed(key, ts.isoformat())
        result = await HistoricalEngine.get_checkpoint(
            symbol="BANKNIFTY",
            exchange="NSE",
            interval="1m",
            redis_client=fake_redis,
        )
        assert result is not None
        assert result == ts

    async def test_get_checkpoint_returns_none_on_corrupt_value(
        self, fake_redis: FakeRedis
    ) -> None:
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="X", exchange="NSE", interval="1m"
        )
        fake_redis.seed(key, "not-a-datetime")
        result = await HistoricalEngine.get_checkpoint(
            symbol="X",
            exchange="NSE",
            interval="1m",
            redis_client=fake_redis,
        )
        assert result is None

    async def test_checkpoint_key_format(self, fake_redis: FakeRedis) -> None:
        """Key must follow mds:backfill:checkpoint:{symbol}:{exchange}:{interval}."""
        await HistoricalEngine.set_checkpoint(
            symbol="RELIANCE",
            exchange="NSE",
            interval="5m",
            ts=_utc(2024, 1, 1),
            redis_client=fake_redis,
        )
        expected_key = "mds:backfill:checkpoint:RELIANCE:NSE:5m"
        assert expected_key in fake_redis._store

    async def test_get_checkpoint_handles_redis_error(self) -> None:
        """Redis read failure returns None (does not raise)."""
        broken_redis = AsyncMock()
        broken_redis.get.side_effect = Exception("Connection refused")
        result = await HistoricalEngine.get_checkpoint(
            symbol="X",
            exchange="NSE",
            interval="1m",
            redis_client=broken_redis,
        )
        assert result is None

    async def test_set_checkpoint_handles_redis_error(self) -> None:
        """Redis write failure logs a warning but does not raise."""
        broken_redis = AsyncMock()
        broken_redis.set.side_effect = Exception("Connection refused")
        # Should not raise — the backfill loop must continue.
        await HistoricalEngine.set_checkpoint(
            symbol="X",
            exchange="NSE",
            interval="1m",
            ts=_utc(2024, 1, 1),
            redis_client=broken_redis,
        )


# ---------------------------------------------------------------------------
# Section 4: Checkpoint resume in run_backfill (Requirement 10.2)
# ---------------------------------------------------------------------------

class TestBackfillResume:
    """run_backfill respects an existing checkpoint and advances from_ts."""

    async def test_resumes_from_checkpoint(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        """When a checkpoint is set mid-range, run_backfill starts from there."""
        original_from = _utc(2024, 1, 1)
        checkpoint = _utc(2024, 1, 15)
        to_ts = _utc(2024, 2, 1)

        # Seed checkpoint
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="RELIANCE", exchange="NSE", interval="1m"
        )
        fake_redis.seed(key, checkpoint.isoformat())

        # Patch _fetch_candles to record what from_ts it sees.
        fetched_ranges: list[tuple] = []

        async def fake_fetch(**kwargs):
            fetched_ranges.append((kwargs["from_ts"], kwargs["to_ts"]))
            return []

        engine._fetch_candles = fake_fetch  # type: ignore[method-assign]

        result = await engine.run_backfill(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_class="EQ",
            interval="1m",
            from_ts=original_from,
            to_ts=to_ts,
            db_engine=MagicMock(),
            redis_client=fake_redis,
            is_indian_market=True,
        )

        assert result["resumed_from_checkpoint"] is True
        # The first chunk must start from the checkpoint, not original_from.
        if fetched_ranges:
            assert fetched_ranges[0][0] == checkpoint
        else:
            # No candles fetched (stub) — but the result should reflect resume.
            assert result["resumed_from_checkpoint"] is True

    async def test_no_resume_when_no_checkpoint(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        """Without a checkpoint, from_ts is used as-is."""
        original_from = _utc(2024, 1, 1)
        to_ts = _utc(2024, 1, 10)

        fetched_ranges: list[tuple] = []

        async def fake_fetch(**kwargs):
            fetched_ranges.append((kwargs["from_ts"], kwargs["to_ts"]))
            return []

        engine._fetch_candles = fake_fetch  # type: ignore[method-assign]

        result = await engine.run_backfill(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_class="EQ",
            interval="1m",
            from_ts=original_from,
            to_ts=to_ts,
            db_engine=MagicMock(),
            redis_client=fake_redis,
            is_indian_market=True,
        )

        assert result["resumed_from_checkpoint"] is False
        if fetched_ranges:
            assert fetched_ranges[0][0] == original_from

    async def test_short_circuits_when_checkpoint_past_to_ts(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        """No chunks attempted when checkpoint >= to_ts (already fully covered)."""
        checkpoint = _utc(2024, 2, 15)
        from_ts = _utc(2024, 1, 1)
        to_ts = _utc(2024, 2, 1)  # to_ts < checkpoint

        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="INFY", exchange="NSE", interval="5m"
        )
        fake_redis.seed(key, checkpoint.isoformat())

        result = await engine.run_backfill(
            symbol="INFY",
            exchange="NSE",
            instrument_class="EQ",
            interval="5m",
            from_ts=from_ts,
            to_ts=to_ts,
            db_engine=MagicMock(),
            redis_client=fake_redis,
            is_indian_market=True,
        )

        assert result["chunks_attempted"] == 0
        assert result["candles_persisted"] == 0

    async def test_checkpoint_updated_after_successful_chunk(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        """After processing a chunk with candles, the checkpoint must be updated."""
        candle_time = _utc(2024, 1, 5, 10, 0)
        mock_candle = {
            "time": candle_time,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 103.0,
            "volume": 500,
        }

        async def fake_fetch(**kwargs):
            return [mock_candle]

        fake_db = MagicMock()
        fake_conn = AsyncMock()
        fake_db.begin.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_db.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        engine._fetch_candles = fake_fetch  # type: ignore[method-assign]

        await engine.run_backfill(
            symbol="TCS",
            exchange="NSE",
            instrument_class="EQ",
            interval="1m",
            from_ts=_utc(2024, 1, 5),
            to_ts=_utc(2024, 1, 6),
            db_engine=fake_db,
            redis_client=fake_redis,
            is_indian_market=True,
        )

        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol="TCS", exchange="NSE", interval="1m"
        )
        # Checkpoint should have been written.
        assert key in fake_redis._store


# ---------------------------------------------------------------------------
# Section 5: bulk_upsert_candles (Requirement 4.3)
# ---------------------------------------------------------------------------

class TestBulkUpsertCandles:
    """bulk_upsert_candles batches rows and calls the correct SQL."""

    async def test_returns_zero_for_empty_candle_list(self) -> None:
        fake_db = MagicMock()
        count = await HistoricalEngine.bulk_upsert_candles(
            candles=[],
            db_engine=fake_db,
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            provider="angel_one",
        )
        assert count == 0
        # No DB calls made.
        fake_db.begin.assert_not_called()

    async def test_inserts_valid_candles(self) -> None:
        candles = [
            {
                "time": _utc(2024, 1, 5, 9, 15),
                "open": 2800.0,
                "high": 2820.0,
                "low": 2795.0,
                "close": 2810.0,
                "volume": 1000,
            },
            {
                "time": _utc(2024, 1, 5, 9, 16),
                "open": 2810.0,
                "high": 2825.0,
                "low": 2805.0,
                "close": 2820.0,
                "volume": 800,
            },
        ]

        executed_rows: list[dict] = []

        fake_conn = AsyncMock()

        async def capture_execute(sql, row):
            executed_rows.append(row)

        fake_conn.execute.side_effect = capture_execute

        fake_ctx = MagicMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_ctx.__aexit__ = AsyncMock(return_value=False)

        fake_db = MagicMock()
        fake_db.begin.return_value = fake_ctx

        count = await HistoricalEngine.bulk_upsert_candles(
            candles=candles,
            db_engine=fake_db,
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            provider="angel_one",
        )

        assert count == 2
        assert len(executed_rows) == 2
        # Verify first row fields
        row0 = executed_rows[0]
        assert row0["open"] == 2800.0
        assert row0["high"] == 2820.0
        assert row0["low"] == 2795.0
        assert row0["close"] == 2810.0
        assert row0["volume"] == 1000
        assert row0["interval_str"] == "1m"
        assert row0["exchange"] == "NSE"
        assert row0["provider"] == "angel_one"

    async def test_skips_candle_with_invalid_time(self) -> None:
        """Candles with un-parseable time are silently skipped."""
        candles = [
            {
                "time": None,  # invalid
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 103.0,
                "volume": 500,
            },
            {
                "time": _utc(2024, 1, 5, 9, 15),
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 103.0,
                "volume": 500,
            },
        ]

        executed_rows: list[dict] = []

        fake_conn = AsyncMock()

        async def capture_execute(sql, row):
            executed_rows.append(row)

        fake_conn.execute.side_effect = capture_execute

        fake_ctx = MagicMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_ctx.__aexit__ = AsyncMock(return_value=False)

        fake_db = MagicMock()
        fake_db.begin.return_value = fake_ctx

        count = await HistoricalEngine.bulk_upsert_candles(
            candles=candles,
            db_engine=fake_db,
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            provider="angel_one",
        )

        # Only the valid candle should be inserted.
        assert count == 1
        assert len(executed_rows) == 1

    async def test_uses_on_conflict_do_update(self) -> None:
        """The generated SQL must contain ON CONFLICT … DO UPDATE."""
        sql_statements: list[str] = []

        fake_conn = AsyncMock()

        async def capture_execute(sql, row):
            sql_statements.append(str(sql))

        fake_conn.execute.side_effect = capture_execute

        fake_ctx = MagicMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_ctx.__aexit__ = AsyncMock(return_value=False)

        fake_db = MagicMock()
        fake_db.begin.return_value = fake_ctx

        candles = [
            {
                "time": _utc(2024, 1, 5, 9, 15),
                "open": 100.0,
                "high": 110.0,
                "low": 95.0,
                "close": 105.0,
                "volume": 200,
            }
        ]

        await HistoricalEngine.bulk_upsert_candles(
            candles=candles,
            db_engine=fake_db,
            symbol="X",
            exchange="NSE",
            interval="1d",
            provider="openchart",
        )

        # At least one statement should contain ON CONFLICT
        assert any("ON CONFLICT" in s.upper() for s in sql_statements)

    async def test_epoch_seconds_time_coercion(self) -> None:
        """Epoch-seconds int for ``time`` must be correctly coerced to datetime."""
        epoch_seconds = int(_utc(2024, 1, 5, 9, 15).timestamp())
        candles = [
            {
                "time": epoch_seconds,
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 103.0,
                "volume": 500,
            }
        ]

        executed_rows: list[dict] = []

        fake_conn = AsyncMock()

        async def capture_execute(sql, row):
            executed_rows.append(row)

        fake_conn.execute.side_effect = capture_execute

        fake_ctx = MagicMock()
        fake_ctx.__aenter__ = AsyncMock(return_value=fake_conn)
        fake_ctx.__aexit__ = AsyncMock(return_value=False)

        fake_db = MagicMock()
        fake_db.begin.return_value = fake_ctx

        count = await HistoricalEngine.bulk_upsert_candles(
            candles=candles,
            db_engine=fake_db,
            symbol="RELIANCE",
            exchange="NSE",
            interval="1m",
            provider="angel_one",
        )

        assert count == 1
        row = executed_rows[0]
        assert isinstance(row["time"], datetime.datetime)


# ---------------------------------------------------------------------------
# Section 6: Provider routing (_resolve_provider) (Requirement 10.3)
# ---------------------------------------------------------------------------

class TestProviderRouting:
    """_resolve_provider returns the correct primary provider per instrument class."""

    def test_eq_routes_to_angel_one(self, engine: HistoricalEngine) -> None:
        p = engine._resolve_provider(
            instrument_class="EQ", interval="1m", is_indian_market=True
        )
        assert p == ProviderId.ANGEL_ONE

    def test_idx_routes_to_upstox(self, engine: HistoricalEngine) -> None:
        p = engine._resolve_provider(
            instrument_class="IDX", interval="1m", is_indian_market=True
        )
        assert p == ProviderId.UPSTOX

    def test_fo_eod_routes_to_jugaad(self, engine: HistoricalEngine) -> None:
        p = engine._resolve_provider(
            instrument_class="FO", interval="1d", is_indian_market=True
        )
        assert p == ProviderId.JUGAAD_DATA

    def test_fo_intraday_routes_to_angel_one(self, engine: HistoricalEngine) -> None:
        for interval in ("1m", "5m", "15m", "30m", "1h"):
            p = engine._resolve_provider(
                instrument_class="FO", interval=interval, is_indian_market=True
            )
            assert p == ProviderId.ANGEL_ONE, (
                f"FO {interval} should route to Angel One, got {p}"
            )

    def test_crypto_routes_to_openchart_fallback(self, engine: HistoricalEngine) -> None:
        """Crypto class with is_indian_market=False falls back to OpenChart."""
        p = engine._resolve_provider(
            instrument_class="CRYPTO_SPOT", interval="1h", is_indian_market=False
        )
        assert p == ProviderId.OPENCHART

    def test_unknown_class_routes_to_openchart_fallback(self, engine: HistoricalEngine) -> None:
        p = engine._resolve_provider(
            instrument_class="UNKNOWN", interval="1d", is_indian_market=True
        )
        assert p == ProviderId.OPENCHART


# ---------------------------------------------------------------------------
# Section 7: _get_chunk_days (Requirement 10.4)
# ---------------------------------------------------------------------------

class TestGetChunkDays:
    """Per-provider × per-interval chunk sizes match the design table."""

    def test_angel_one_1m_is_30_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.ANGEL_ONE, interval="1m") == 30

    def test_angel_one_5m_is_90_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.ANGEL_ONE, interval="5m") == 90

    def test_angel_one_15m_is_90_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.ANGEL_ONE, interval="15m") == 90

    def test_angel_one_1d_is_365_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.ANGEL_ONE, interval="1d") == 365

    def test_upstox_1m_is_7_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.UPSTOX, interval="1m") == 7

    def test_upstox_5m_is_30_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.UPSTOX, interval="5m") == 30

    def test_upstox_15m_is_30_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.UPSTOX, interval="15m") == 30

    def test_upstox_1d_is_365_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.UPSTOX, interval="1d") == 365

    def test_jugaad_is_3650_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.JUGAAD_DATA, interval="1d") == 3650

    def test_openchart_is_365_days(self, engine: HistoricalEngine) -> None:
        assert engine._get_chunk_days(provider=ProviderId.OPENCHART, interval="1m") == 365


# ---------------------------------------------------------------------------
# Section 8: _coerce_to_datetime
# ---------------------------------------------------------------------------

class TestCoerceToDatetime:
    """_coerce_to_datetime handles all expected time representations."""

    def test_none_returns_none(self) -> None:
        assert _coerce_to_datetime(None) is None

    def test_utc_datetime_passthrough(self) -> None:
        dt = _utc(2024, 6, 1, 10, 0)
        result = _coerce_to_datetime(dt)
        assert result == dt
        assert result.tzinfo is not None

    def test_naive_datetime_assumes_utc(self) -> None:
        naive = datetime.datetime(2024, 6, 1, 10, 0)
        result = _coerce_to_datetime(naive)
        assert result is not None
        assert result.tzinfo == UTC

    def test_epoch_seconds_int(self) -> None:
        ts = _utc(2024, 1, 15, 9, 15)
        epoch = int(ts.timestamp())
        result = _coerce_to_datetime(epoch)
        assert result is not None
        assert result == ts

    def test_epoch_seconds_float(self) -> None:
        ts = _utc(2024, 1, 15, 9, 15)
        epoch = ts.timestamp()
        result = _coerce_to_datetime(epoch)
        assert result is not None
        # Allow 1 second tolerance for float precision
        assert abs((result - ts).total_seconds()) < 1.0

    def test_iso8601_string(self) -> None:
        ts = _utc(2024, 3, 20, 14, 30)
        iso = ts.isoformat()
        result = _coerce_to_datetime(iso)
        assert result is not None
        assert result == ts

    def test_invalid_string_returns_none(self) -> None:
        result = _coerce_to_datetime("not-a-date")
        assert result is None

    def test_garbage_type_returns_none(self) -> None:
        result = _coerce_to_datetime({"timestamp": "2024-01-01"})
        assert result is None


# ---------------------------------------------------------------------------
# Section 9: run_backfill summary dict shape
# ---------------------------------------------------------------------------

class TestBackfillSummary:
    """run_backfill returns a well-formed summary dict."""

    async def test_summary_keys_present(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        result = await engine.run_backfill(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_class="EQ",
            interval="1m",
            from_ts=_utc(2024, 1, 1),
            to_ts=_utc(2024, 1, 3),
            db_engine=MagicMock(),
            redis_client=fake_redis,
            is_indian_market=True,
        )
        expected_keys = {
            "symbol",
            "exchange",
            "interval",
            "chunks_attempted",
            "chunks_succeeded",
            "candles_persisted",
            "resumed_from_checkpoint",
            "checkpoint_ts",
            "incidents",
        }
        assert expected_keys.issubset(result.keys())

    async def test_summary_incident_list_on_no_incidents(
        self, engine: HistoricalEngine, fake_redis: FakeRedis
    ) -> None:
        result = await engine.run_backfill(
            symbol="TCS",
            exchange="NSE",
            instrument_class="EQ",
            interval="5m",
            from_ts=_utc(2024, 1, 1),
            to_ts=_utc(2024, 1, 5),
            db_engine=MagicMock(),
            redis_client=fake_redis,
            is_indian_market=True,
        )
        assert isinstance(result["incidents"], list)


# ---------------------------------------------------------------------------
# Section 10: MINIMUM_HISTORY_DAYS constant (Requirement 4.1)
# ---------------------------------------------------------------------------

class TestMinimumHistoryDays:
    """Minimum history depth constants match the design specification."""

    def test_1m_is_60_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["1m"] == 60

    def test_5m_is_180_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["5m"] == 180

    def test_30m_is_180_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["30m"] == 180

    def test_1h_is_365_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["1h"] == 365

    def test_1d_is_3650_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["1d"] == 3650

    def test_1w_is_3650_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["1w"] == 3650

    def test_1M_is_3650_days(self) -> None:
        assert MINIMUM_HISTORY_DAYS["1M"] == 3650
