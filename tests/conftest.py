"""
tests/conftest.py
==================

Root conftest for the DATA-SERVICE 2.0 test suite.

This file re-exports all shared fixtures defined in ``tests/fixtures/conftest.py``
so that they are available to every test module under ``tests/`` — regardless of
which subdirectory the test file lives in.

Adding a new shared fixture
---------------------------
1. Define it in ``tests/fixtures/conftest.py``.
2. Import and re-export it here with ``from tests.fixtures.conftest import <name>``.

Note: pytest discovers ``conftest.py`` files by walking up from the collected
test file to the rootdir.  By placing fixtures in this top-level conftest they
are guaranteed to be available everywhere.
"""

from __future__ import annotations

# Re-export all shared fixtures from the fixtures package so they are
# auto-discovered by pytest for every test under tests/.
from tests.fixtures.conftest import (  # noqa: F401
    angel_one_provider,
    binance_client,
    deribit_client,
    deribit_ticker,
    mock_redis,
    option_chain_row,
    sample_india_ticks,
    sample_ohlcv_klines,
    valid_ohlcv_candle,
    valid_tick,
)
