"""
Unit tests for src/providers/delta_persistence.py

Mirrors test_binance_persistence.py.

Requirements: DS2-RCA-001
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.delta_normaliser import DeltaCandleRecord
from src.providers.delta_persistence import DeltaPersistenceLayer


def _record(**overrides) -> DeltaCandleRecord:
    defaults = dict(
        symbol="BTCUSD",
        interval="1h",
        time=1_700_000_000_000,
        open=43_000.0,
        high=43_500.0,
        low=42_800.0,
        close=43_200.0,
        volume=15.5,
        closeTime=1_700_003_599_999,
    )
    defaults.update(overrides)
    return DeltaCandleRecord(**defaults)


class TestDeltaPersistenceLayer:
    def setup_method(self) -> None:
        self.layer = DeltaPersistenceLayer()

    @pytest.mark.asyncio
    async def test_returns_zero_when_engine_is_none(self) -> None:
        count = await self.layer.persist([_record()], db_engine=None)
        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_list(self) -> None:
        mock_engine = AsyncMock()
        count = await self.layer.persist([], db_engine=mock_engine)
        assert count == 0
        mock_engine.begin.assert_not_called()

    @pytest.mark.asyncio
    async def test_persists_records_via_upsert(self) -> None:
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_engine = MagicMock()
        mock_engine.begin = MagicMock(return_value=mock_ctx)

        records = [_record(), _record(symbol="ETHUSD")]
        count = await self.layer.persist(records, db_engine=mock_engine)
        assert count == 2
        assert mock_conn.execute.call_count == 2

    def test_to_row_exchange_is_delta(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record())
        assert row["exchange"] == "DELTA"

    def test_to_row_provider_is_delta(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record())
        assert row["provider"] == "delta"

    def test_to_row_time_is_datetime(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record())
        assert isinstance(row["time"], datetime)
        assert row["time"].tzinfo is not None

    def test_to_row_volume_is_int(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record(volume=15.7))
        assert isinstance(row["volume"], int)
        assert row["volume"] == 15

    def test_to_row_oi_is_none(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record())
        assert row["oi"] is None

    def test_to_row_source_type_credential_free(self) -> None:
        row = DeltaPersistenceLayer._to_row(_record())
        assert row["source_type"] == "CREDENTIAL_FREE"
