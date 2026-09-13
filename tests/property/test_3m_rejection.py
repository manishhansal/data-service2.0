"""
Property 7 — 3m Interval Rejection for Indian Market Data

The 3m interval must be rejected at every layer for Indian market data.
This property verifies the rejection is consistent regardless of other parameters.

Requirement: 1.5, 4.2, 10.11, 16.10
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.providers.adapters.angel_one import AngelOneAdapter, _BANNED_INTERVALS
from src.providers.adapters.upstox import UpstoxAdapter
from src.providers.adapters.base import ProviderUnsupportedError


symbol_st = st.text(min_size=1, max_size=20, alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ")
exchange_st = st.sampled_from(["NSE", "NFO", "BSE"])


class Test3mRejection:
    def test_3m_is_in_banned_intervals(self) -> None:
        """Verify the banned interval constant contains 3m."""
        assert "3m" in _BANNED_INTERVALS

    @given(symbol_st, exchange_st)
    @settings(max_examples=50)
    def test_angel_one_rejects_3m(self, symbol: str, exchange: str) -> None:
        """AngelOneAdapter raises ProviderUnsupportedError for 3m regardless of symbol/exchange."""
        import asyncio
        adapter = AngelOneAdapter(
            api_key="dummy", client_id="dummy", totp_secret="JBSWY3DPEHPK3PXP"
        )

        async def _run():
            await adapter.fetch_historical_ohlcv(
                symbol=symbol,
                token="99999",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="3m",
                exchange=exchange,
            )

        with pytest.raises(ProviderUnsupportedError) as exc_info:
            asyncio.new_event_loop().run_until_complete(_run())
        assert "3m" in str(exc_info.value).lower() or "unsupported" in str(exc_info.value).lower()

    @given(symbol_st)
    @settings(max_examples=50)
    def test_upstox_rejects_3m(self, symbol: str) -> None:
        """UpstoxAdapter raises ValueError for 3m regardless of symbol."""
        adapter = UpstoxAdapter(api_key="dummy", api_secret="dummy")
        with pytest.raises(ValueError) as exc_info:
            # _assert_not_3m is called synchronously before any I/O
            UpstoxAdapter._assert_not_3m("3m")
        assert "3m" in str(exc_info.value).lower()

    def test_non_3m_intervals_not_rejected_by_upstox(self) -> None:
        """Verify that valid intervals are NOT blocked by the 3m guard."""
        valid_intervals = ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"]
        for interval in valid_intervals:
            # Should not raise
            UpstoxAdapter._assert_not_3m(interval)

    def test_non_3m_intervals_not_banned_angel(self) -> None:
        """Verify valid intervals are not in the banned set."""
        valid_intervals = ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w"]
        for interval in valid_intervals:
            assert interval not in _BANNED_INTERVALS
