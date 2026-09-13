"""
tests/mocks
===========

Reusable provider mock fixtures and factory functions for DATA-SERVICE 2.0
integration and unit tests.

Exports
-------
- MockAngelOneProvider
- MockBinanceClient
- MockDeribitClient
- MockRedis
- make_valid_tick
- make_valid_ohlcv_candle
- make_option_chain_row
- make_deribit_ticker
- SAMPLE_OHLCV_KLINES
- SAMPLE_INDIA_TICKS
"""

from tests.mocks.provider_mocks import (
    SAMPLE_INDIA_TICKS,
    SAMPLE_OHLCV_KLINES,
    MockAngelOneProvider,
    MockBinanceClient,
    MockDeribitClient,
    MockRedis,
    make_deribit_ticker,
    make_option_chain_row,
    make_valid_ohlcv_candle,
    make_valid_tick,
)

__all__ = [
    "MockAngelOneProvider",
    "MockBinanceClient",
    "MockDeribitClient",
    "MockRedis",
    "make_valid_tick",
    "make_valid_ohlcv_candle",
    "make_option_chain_row",
    "make_deribit_ticker",
    "SAMPLE_OHLCV_KLINES",
    "SAMPLE_INDIA_TICKS",
]
