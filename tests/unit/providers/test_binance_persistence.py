"""
Unit tests for BinancePersistenceLayer.

Covers:
- Returns 0 and does not raise when db_engine is None
- Returns 0 for empty candle list
- Calls db_engine.begin() once per batch
- Passes correct SQL and row parameters for each candle
- _to_row converts epoch ms → datetime, derives session_date and session fields
- Row-level SQLAlchemy errors are caught; remaining rows are still attempted
- Returns count of successfully persisted rows
- poor_quality flag is forwarded to the row
- oi is always None (not available in kline responses)
- volume is truncated to int in the row dict

Requirements: 13.8, 13.9
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.binance_normaliser import BinanceCandleRecord
from src.providers.binance_persistence import BinancePersistenceLayer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    time: int = 1_700_000_000_000,
    open: float = 30_000.0,
    high: float = 30_500.0,
    low: float = 29_800.0,
    close: float = 30_200.0,
    volume: float = 150.75,
    close_time: int = 1_700_003_599_999,
    poor_quality: bool = False,
) -> BinanceCandleRecord:
    return BinanceCandleRecord(
        symbol=symbol,
        interval=interval,
        time=time,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        closeTime=close_time,
        poor_quality=poor_quality,
    )


def _make_mock_engine(*, row_side_effect=None) -> MagicMock:
    """Build a mock async SQLAlchemy engine.

    The mock implements ``engine.begin()`` as an async context manager that
    yields a mock connection whose ``execute()`` is an AsyncMock.
    """
    conn = AsyncMock()
    if row_side_effect is not None:
        conn.execute.side_effect = row_side_effect

    # engine.begin() is an async context manager
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()
    engine.begin.return_value = cm
    return engine, conn


# ---------------------------------------------------------------------------
# No-engine / empty-batch guards
# ---------------------------------------------------------------------------


class TestPersistGuards:
    @pytest.mark.asyncio
    async def test_none_engine_returns_zero(self) -> None:
        layer = BinancePersistenceLayer()
        records = [_make_record()]
        result = await layer.persist(records, db_engine=None)
        assert result == 0

    @pytest.mark.asyncio
    async def test_none_engine_does_not_raise(self) -> None:
        layer = BinancePersistenceLayer()
        # Should not raise even with multiple records
        await layer.persist([_make_record(), _make_record()], db_engine=None)

    @pytest.mark.asyncio
    async def test_empty_candles_returns_zero(self) -> None:
        layer = BinancePersistenceLayer()
        engine, conn = _make_mock_engine()
        result = await layer.persist([], db_engine=engine)
        assert result == 0
        # Should not open a connection for an empty batch
        conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Normal persistence
# ---------------------------------------------------------------------------


class TestPersistSuccess:
    @pytest.mark.asyncio
    async def test_single_record_persisted(self) -> None:
        engine, conn = _make_mock_engine()
        layer = BinancePersistenceLayer()
        records = [_make_record()]

        count = await layer.persist(records, db_engine=engine)
        assert count == 1
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_records_all_persisted(self) -> None:
        engine, conn = _make_mock_engine()
        layer = BinancePersistenceLayer()
        records = [
            _make_record(time=1_700_000_000_000 + i * 3_600_000)
            for i in range(5)
        ]

        count = await layer.persist(records, db_engine=engine)
        assert count == 5
        assert conn.execute.call_count == 5

    @pytest.mark.asyncio
    async def test_engine_begin_called_once(self) -> None:
        engine, conn = _make_mock_engine()
        layer = BinancePersistenceLayer()
        records = [_make_record(), _make_record(time=1_700_003_600_000)]

        await layer.persist(records, db_engine=engine)
        engine.begin.assert_called_once()


# ---------------------------------------------------------------------------
# Row parameter correctness (_to_row)
# ---------------------------------------------------------------------------


class TestToRow:
    """Validate the parameter dict passed to conn.execute()."""

    def _capture_row(self) -> tuple[MagicMock, MagicMock, list]:
        """Return (engine, conn, captured_rows) — rows captured on execute."""
        captured: list = []

        async def capture_execute(sql, row):
            captured.append(row)

        conn = AsyncMock()
        conn.execute.side_effect = capture_execute

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.begin.return_value = cm
        return engine, conn, captured

    @pytest.mark.asyncio
    async def test_instrument_id_is_symbol(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record(symbol="BTCUSDT")], db_engine=engine)
        assert captured[0]["instrument_id"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_exchange_is_binance(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["exchange"] == "BINANCE"

    @pytest.mark.asyncio
    async def test_interval_str_is_interval(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record(interval="4h")], db_engine=engine)
        assert captured[0]["interval_str"] == "4h"

    @pytest.mark.asyncio
    async def test_time_is_utc_datetime(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        epoch_ms = 1_700_000_000_000
        await layer.persist([_make_record(time=epoch_ms)], db_engine=engine)
        t = captured[0]["time"]
        assert isinstance(t, datetime.datetime)
        assert t.tzinfo == datetime.timezone.utc
        expected = datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.timezone.utc)
        assert t == expected

    @pytest.mark.asyncio
    async def test_session_date_matches_utc_date_of_time(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        epoch_ms = 1_700_000_000_000
        await layer.persist([_make_record(time=epoch_ms)], db_engine=engine)
        expected_date = datetime.datetime.fromtimestamp(
            epoch_ms / 1000, tz=datetime.timezone.utc
        ).date()
        assert captured[0]["session_date"] == expected_date

    @pytest.mark.asyncio
    async def test_oi_is_none(self) -> None:
        """OI is not available in kline responses — must be None."""
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["oi"] is None

    @pytest.mark.asyncio
    async def test_volume_is_truncated_to_int(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record(volume=150.75)], db_engine=engine)
        # BigInteger column — fractional part is truncated
        assert captured[0]["volume"] == 150
        assert isinstance(captured[0]["volume"], int)

    @pytest.mark.asyncio
    async def test_poor_quality_false_by_default(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record(poor_quality=False)], db_engine=engine)
        assert captured[0]["poor_quality"] is False

    @pytest.mark.asyncio
    async def test_poor_quality_true_forwarded(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record(poor_quality=True)], db_engine=engine)
        assert captured[0]["poor_quality"] is True

    @pytest.mark.asyncio
    async def test_provider_is_binance(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["provider"] == "binance"

    @pytest.mark.asyncio
    async def test_source_type_is_credential_free(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["source_type"] == "CREDENTIAL_FREE"

    @pytest.mark.asyncio
    async def test_volume_unavailable_is_false(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["volume_unavailable"] is False

    @pytest.mark.asyncio
    async def test_normalisation_version_present(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["normalisation_version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_dataset_version_is_1(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        await layer.persist([_make_record()], db_engine=engine)
        assert captured[0]["dataset_version"] == 1

    @pytest.mark.asyncio
    async def test_ohlc_values_forwarded_correctly(self) -> None:
        engine, conn, captured = self._capture_row()
        layer = BinancePersistenceLayer()
        record = _make_record(open=100.1, high=105.5, low=98.3, close=103.7)
        await layer.persist([record], db_engine=engine)
        row = captured[0]
        assert row["open"] == 100.1
        assert row["high"] == 105.5
        assert row["low"] == 98.3
        assert row["close"] == 103.7


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestPersistErrorHandling:
    @pytest.mark.asyncio
    async def test_row_error_does_not_abort_batch(self) -> None:
        """A per-row SQLAlchemy error should be caught; other rows still run."""
        from sqlalchemy.exc import SQLAlchemyError

        call_count = 0

        async def execute_side_effect(sql, row):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise SQLAlchemyError("simulated row error")

        conn = AsyncMock()
        conn.execute.side_effect = execute_side_effect

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.begin.return_value = cm

        layer = BinancePersistenceLayer()
        records = [
            _make_record(time=1_700_000_000_000),
            _make_record(time=1_700_003_600_000),  # this row raises
            _make_record(time=1_700_007_200_000),
        ]

        count = await layer.persist(records, db_engine=engine)
        # Rows 1 and 3 succeeded; row 2 failed
        assert count == 2
        # execute was still called for all 3 rows
        assert conn.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_connection_error_returns_zero(self) -> None:
        """A connection-level error (engine.begin() raises) returns 0."""
        from sqlalchemy.exc import SQLAlchemyError

        engine = MagicMock()
        cm = AsyncMock()
        # __aenter__ raises to simulate connection failure
        cm.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("connection failed"))
        cm.__aexit__ = AsyncMock(return_value=False)
        engine.begin.return_value = cm

        layer = BinancePersistenceLayer()
        records = [_make_record()]
        count = await layer.persist(records, db_engine=engine)
        assert count == 0

    @pytest.mark.asyncio
    async def test_all_rows_fail_returns_zero(self) -> None:
        """If every row raises, the count is 0."""
        from sqlalchemy.exc import SQLAlchemyError

        conn = AsyncMock()
        conn.execute.side_effect = SQLAlchemyError("always fails")

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.begin.return_value = cm

        layer = BinancePersistenceLayer()
        records = [_make_record(time=1_700_000_000_000 + i * 3_600_000) for i in range(3)]
        count = await layer.persist(records, db_engine=engine)
        assert count == 0


# ---------------------------------------------------------------------------
# _to_row direct unit test
# ---------------------------------------------------------------------------


class TestToRowDirect:
    """Test _to_row as a static method without going through persist()."""

    def test_minimal_record(self) -> None:
        record = _make_record()
        row = BinancePersistenceLayer._to_row(record)

        assert row["instrument_id"] == "BTCUSDT"
        assert row["exchange"] == "BINANCE"
        assert row["interval_str"] == "1h"
        assert isinstance(row["time"], datetime.datetime)
        assert row["time"].tzinfo == datetime.timezone.utc
        assert row["open"] == 30_000.0
        assert row["high"] == 30_500.0
        assert row["low"] == 29_800.0
        assert row["close"] == 30_200.0
        assert row["volume"] == 150  # truncated
        assert row["volume_unavailable"] is False
        assert row["oi"] is None
        assert row["provider"] == "binance"
        assert row["source_type"] == "CREDENTIAL_FREE"
        assert row["normalisation_version"] == "2.0.0"
        assert row["dataset_version"] == 1
        assert row["poor_quality"] is False
        assert row["data_observation_id"] is None
        assert row["reconciliation_status"] is None

    def test_3m_interval_row(self) -> None:
        record = _make_record(interval="3m")
        row = BinancePersistenceLayer._to_row(record)
        assert row["interval_str"] == "3m"

    def test_session_date_type_is_date(self) -> None:
        record = _make_record()
        row = BinancePersistenceLayer._to_row(record)
        assert isinstance(row["session_date"], datetime.date)
