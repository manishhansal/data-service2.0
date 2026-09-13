"""
Unit tests for src/providers/deribit_client.py — Task 12.1.

Covers:
- Provider constants (PROVIDER_ID, DERIBIT_CURRENCIES).
- OHLCV normalisation: TradingView parallel arrays → list of canonical dicts.
  - Empty arrays produce empty list.
  - Mismatched array lengths raise ValueError.
- get_instruments: correct URL, params (currency uppercased, kind param),
  returns list.
- get_order_book: correct URL, instrument_name param, null fields preserved.
- get_ohlcv: correct URL, params, normalised output.
- get_index_price: correct URL, index_name param, index_name injected when
  absent from result.
- get_ticker: correct URL, instrument_name param, null fields preserved.
- JSON-RPC unwrapping: ``result`` is extracted from the ``{"jsonrpc": ...,
  "result": ...}`` envelope.
- Deribit application errors: ``{"error": {"code": N, "message": "..."}}``
  raises ValueError with the message.
- HTTP error handling:
  - 4xx raised immediately (no retry).
  - 429 raised immediately (no retry; gateway handles Retry-After).
  - 5xx retried up to max_retries; final exception propagated.
  - 5xx succeeds on a later attempt.
- Context manager: close() on __aexit__; injected session not closed.
- Default and custom base URLs.

Requirements: 14.1, 14.2, 14.5, 14.8
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.providers.deribit_client import (
    DERIBIT_CURRENCIES,
    PROVIDER_ID,
    DeribitClient,
    _normalise_ohlcv,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonrpc_ok(result: Any, req_id: int = 1) -> dict[str, Any]:
    """Build a successful Deribit JSON-RPC response envelope."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(code: int, message: str, req_id: int = 1) -> dict[str, Any]:
    """Build a Deribit JSON-RPC error response envelope."""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _mock_response(
    status_code: int = 200,
    json_body: Any = None,
) -> MagicMock:
    """Build a mock ``httpx.Response``."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    if json_body is not None:
        mock.json.return_value = json_body
    else:
        mock.json.side_effect = ValueError("no body")

    if status_code >= 400:
        request = httpx.Request("GET", "https://www.deribit.com/api/v2/test")
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=request,
            response=mock,
        )
    else:
        mock.raise_for_status.return_value = None
    return mock


def _client_with_mock_session(mock_session: AsyncMock) -> DeribitClient:
    """Create a DeribitClient pre-wired with a mock session."""
    return DeribitClient(session=mock_session)


def _make_tv_ohlcv(
    ticks: list[int] | None = None,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    closes: list[float] | None = None,
    volumes: list[float] | None = None,
) -> dict[str, Any]:
    """Build a minimal Deribit TradingView chart data payload."""
    return {
        "ticks":  ticks  or [1705300000000, 1705303600000],
        "open":   opens  or [65000.0, 65100.0],
        "high":   highs  or [65500.0, 65600.0],
        "low":    lows   or [64800.0, 64900.0],
        "close":  closes or [65200.0, 65300.0],
        "volume": volumes or [10.5, 12.3],
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# Provider constants
# ---------------------------------------------------------------------------


class TestProviderConstants:
    def test_provider_id_is_deribit(self) -> None:
        assert PROVIDER_ID == "deribit"

    def test_deribit_currencies_contains_btc(self) -> None:
        assert "BTC" in DERIBIT_CURRENCIES

    def test_deribit_currencies_contains_eth(self) -> None:
        assert "ETH" in DERIBIT_CURRENCIES

    def test_deribit_currencies_contains_sol(self) -> None:
        assert "SOL" in DERIBIT_CURRENCIES

    def test_deribit_currencies_has_exactly_three(self) -> None:
        assert len(DERIBIT_CURRENCIES) == 3


# ---------------------------------------------------------------------------
# OHLCV normalisation (_normalise_ohlcv)
# ---------------------------------------------------------------------------


class TestNormaliseOhlcv:
    def test_returns_list_of_dicts(self) -> None:
        result = _normalise_ohlcv(_make_tv_ohlcv())
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)

    def test_canonical_keys_present(self) -> None:
        result = _normalise_ohlcv(_make_tv_ohlcv())
        for item in result:
            assert set(item.keys()) == {"time", "open", "high", "low", "close", "volume"}

    def test_length_matches_input_ticks(self) -> None:
        tv = _make_tv_ohlcv(
            ticks=[1705300000000, 1705303600000, 1705307200000],
            opens=[1.0, 2.0, 3.0],
            highs=[1.5, 2.5, 3.5],
            lows=[0.9, 1.9, 2.9],
            closes=[1.2, 2.2, 3.2],
            volumes=[100.0, 200.0, 300.0],
        )
        assert len(_normalise_ohlcv(tv)) == 3

    def test_time_is_epoch_ms_int(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert result[0]["time"] == 1705300000000
        assert isinstance(result[0]["time"], int)

    def test_open_is_float(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert isinstance(result[0]["open"], float)
        assert result[0]["open"] == pytest.approx(65000.0)

    def test_high_is_float(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert result[0]["high"] == pytest.approx(65500.0)

    def test_low_is_float(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert result[0]["low"] == pytest.approx(64800.0)

    def test_close_is_float(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert result[0]["close"] == pytest.approx(65200.0)

    def test_volume_is_float(self) -> None:
        result = _normalise_ohlcv(
            _make_tv_ohlcv(
                ticks=[1705300000000], opens=[65000.0], highs=[65500.0],
                lows=[64800.0], closes=[65200.0], volumes=[10.5],
            )
        )
        assert result[0]["volume"] == pytest.approx(10.5)

    def test_empty_arrays_return_empty_list(self) -> None:
        tv = {
            "ticks": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "status": "ok",
        }
        assert _normalise_ohlcv(tv) == []

    def test_mismatched_array_lengths_raise_value_error(self) -> None:
        tv = {
            "ticks":  [1705300000000, 1705303600000],   # 2 entries
            "open":   [65000.0],                         # 1 entry — mismatch
            "high":   [65500.0, 65600.0],
            "low":    [64800.0, 64900.0],
            "close":  [65200.0, 65300.0],
            "volume": [10.5, 12.3],
        }
        with pytest.raises(ValueError, match="mismatched lengths"):
            _normalise_ohlcv(tv)

    def test_missing_arrays_treated_as_empty(self) -> None:
        """Missing keys default to empty lists → empty normalised output."""
        result = _normalise_ohlcv({"status": "ok"})
        assert result == []

    def test_ordering_preserved(self) -> None:
        tv = _make_tv_ohlcv(
            ticks=[1705300000000, 1705303600000],
            opens=[100.0, 200.0],
            highs=[110.0, 210.0],
            lows=[90.0, 190.0],
            closes=[105.0, 205.0],
            volumes=[1.0, 2.0],
        )
        result = _normalise_ohlcv(tv)
        assert result[0]["time"] == 1705300000000
        assert result[1]["time"] == 1705303600000
        assert result[0]["open"] == pytest.approx(100.0)
        assert result[1]["open"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# get_instruments
# ---------------------------------------------------------------------------


class TestGetInstruments:
    async def test_returns_list(self) -> None:
        instruments = [
            {"instrument_name": "BTC-27DEC24-100000-C", "kind": "option"},
            {"instrument_name": "BTC-27DEC24-90000-P", "kind": "option"},
        ]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(instruments))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_instruments("BTC", kind="option")

        assert isinstance(result, list)
        assert len(result) == 2

    async def test_url_uses_public_get_instruments(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok([]))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_instruments("ETH")

        call_url = mock_session.get.call_args[0][0]
        assert "/public/get_instruments" in call_url

    async def test_currency_uppercased(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok([]))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_instruments("btc")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["currency"] == "BTC"

    async def test_kind_param_passed(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok([]))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_instruments("SOL", kind="future")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["kind"] == "future"

    async def test_default_kind_is_option(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok([]))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_instruments("BTC")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["kind"] == "option"

    async def test_empty_result_returns_empty_list(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok([]))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_instruments("BTC")
        assert result == []


# ---------------------------------------------------------------------------
# get_order_book
# ---------------------------------------------------------------------------


class TestGetOrderBook:
    async def test_url_uses_public_get_order_book(self) -> None:
        payload = {
            "instrument_name": "BTC-27DEC24-100000-C",
            "mark_price": 0.05,
            "mark_iv": None,
            "open_interest": None,
            "bids": [],
            "asks": [],
        }
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_order_book("BTC-27DEC24-100000-C")

        call_url = mock_session.get.call_args[0][0]
        assert "/public/get_order_book" in call_url

    async def test_instrument_name_param_passed(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({}))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_order_book("ETH-PERPETUAL")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["instrument_name"] == "ETH-PERPETUAL"

    async def test_null_fields_preserved(self) -> None:
        """Null values from Deribit must NOT be substituted with zero."""
        payload = {
            "mark_iv": None,
            "open_interest": None,
            "underlying_price": None,
        }
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_order_book("BTC-27DEC24-100000-C")

        assert result["mark_iv"] is None
        assert result["open_interest"] is None
        assert result["underlying_price"] is None

    async def test_returns_dict(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({"mark_price": 0.12}))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_order_book("BTC-27DEC24-100000-C")
        assert isinstance(result, dict)
        assert result["mark_price"] == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# get_ohlcv
# ---------------------------------------------------------------------------


class TestGetOhlcv:
    async def test_url_uses_get_tradingview_chart_data(self) -> None:
        tv = _make_tv_ohlcv()
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(tv))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ohlcv("BTC-PERPETUAL", "60", 1705300000000, 1705386400000)

        call_url = mock_session.get.call_args[0][0]
        assert "/public/get_tradingview_chart_data" in call_url

    async def test_params_passed_correctly(self) -> None:
        tv = _make_tv_ohlcv()
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(tv))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ohlcv("BTC-PERPETUAL", "1D", 1705300000000, 1705386400000)

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["instrument_name"] == "BTC-PERPETUAL"
        assert params["resolution"] == "1D"
        assert params["start_timestamp"] == 1705300000000
        assert params["end_timestamp"] == 1705386400000

    async def test_returns_normalised_candle_list(self) -> None:
        tv = _make_tv_ohlcv(
            ticks=[1705300000000, 1705303600000],
            opens=[65000.0, 65100.0],
            highs=[65500.0, 65600.0],
            lows=[64800.0, 64900.0],
            closes=[65200.0, 65300.0],
            volumes=[10.0, 12.0],
        )
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(tv))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_ohlcv("BTC-PERPETUAL", "60", 1705300000000, 1705386400000)

        assert len(result) == 2
        assert result[0]["time"] == 1705300000000
        assert result[0]["open"] == pytest.approx(65000.0)
        assert set(result[0].keys()) == {"time", "open", "high", "low", "close", "volume"}

    async def test_empty_result_returns_empty_list(self) -> None:
        tv = {
            "ticks": [], "open": [], "high": [], "low": [], "close": [], "volume": [],
            "status": "ok",
        }
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(tv))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_ohlcv("BTC-PERPETUAL", "60", 0, 1)
        assert result == []

    async def test_3m_resolution_not_banned(self) -> None:
        """3m is valid for Deribit crypto data — no restriction applies."""
        tv = _make_tv_ohlcv()
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(tv))
        )

        client = _client_with_mock_session(mock_session)
        # Must not raise any error for 3m resolution.
        result = await client.get_ohlcv("BTC-PERPETUAL", "3", 0, 1)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# get_index_price
# ---------------------------------------------------------------------------


class TestGetIndexPrice:
    async def test_url_uses_public_get_index_price(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({"index_price": 65123.45}))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_index_price("btc_usd")

        call_url = mock_session.get.call_args[0][0]
        assert "/public/get_index_price" in call_url

    async def test_index_name_param_passed(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({"index_price": 3000.0}))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_index_price("eth_usd")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["index_name"] == "eth_usd"

    async def test_returns_index_price_as_float(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({"index_price": 65123.45}))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_index_price("btc_usd")

        assert result["index_price"] == pytest.approx(65123.45)

    async def test_index_name_injected_when_absent_from_result(self) -> None:
        """When Deribit omits index_name from result, the client injects it."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(
                200, _jsonrpc_ok({"index_price": 65000.0})  # no index_name key
            )
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_index_price("btc_usd")

        assert result["index_name"] == "btc_usd"
        assert result["index_price"] == pytest.approx(65000.0)

    async def test_index_name_not_overwritten_when_present(self) -> None:
        """If Deribit includes index_name, the client must not overwrite it."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(
                200,
                _jsonrpc_ok({"index_name": "btc_usd", "index_price": 65000.0}),
            )
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_index_price("btc_usd")

        assert result["index_name"] == "btc_usd"


# ---------------------------------------------------------------------------
# get_ticker
# ---------------------------------------------------------------------------


class TestGetTicker:
    async def test_url_uses_public_ticker(self) -> None:
        payload = {"mark_price": 0.05, "mark_iv": 80.0, "open_interest": 1234.5}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ticker("BTC-27DEC24-100000-C")

        call_url = mock_session.get.call_args[0][0]
        assert "/public/ticker" in call_url

    async def test_instrument_name_param_passed(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok({}))
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ticker("ETH-PERPETUAL")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["instrument_name"] == "ETH-PERPETUAL"

    async def test_null_mark_iv_preserved(self) -> None:
        """mark_iv: null must be returned as None, not 0."""
        payload = {"mark_price": 0.1, "mark_iv": None, "open_interest": None}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_ticker("BTC-27DEC24-100000-C")

        assert result["mark_iv"] is None

    async def test_null_open_interest_preserved(self) -> None:
        """open_interest: null must be returned as None, not 0."""
        payload = {"mark_price": 0.1, "mark_iv": None, "open_interest": None}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_ticker("BTC-27DEC24-100000-C")

        assert result["open_interest"] is None

    async def test_returns_full_dict(self) -> None:
        payload = {
            "mark_price": 0.05,
            "mark_iv": 80.5,
            "best_bid_price": 0.049,
            "best_ask_price": 0.051,
            "open_interest": 123.45,
        }
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_ticker("BTC-27DEC24-100000-C")

        assert result["mark_iv"] == pytest.approx(80.5)
        assert result["best_bid_price"] == pytest.approx(0.049)
        assert result["best_ask_price"] == pytest.approx(0.051)


# ---------------------------------------------------------------------------
# JSON-RPC unwrapping
# ---------------------------------------------------------------------------


class TestJsonRpcUnwrapping:
    async def test_result_field_extracted(self) -> None:
        """The client must return the ``result`` value, not the full envelope."""
        instruments = [{"instrument_name": "BTC-PERPETUAL"}]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(instruments))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_instruments("BTC")

        assert result == instruments  # not {"jsonrpc": ..., "result": [...]}

    async def test_result_can_be_dict(self) -> None:
        payload = {"index_price": 65000.0, "index_name": "btc_usd"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(payload))
        )

        client = _client_with_mock_session(mock_session)
        result = await client.get_index_price("btc_usd")

        assert isinstance(result, dict)
        assert result["index_price"] == pytest.approx(65000.0)

    async def test_result_none_returns_none(self) -> None:
        """If Deribit returns ``"result": null`` the client returns None."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_ok(None))
        )

        client = DeribitClient(session=mock_session)
        # Use internal _get directly for this edge case.
        result = await client._get(f"{client._base_url}/public/some_endpoint")
        assert result is None


# ---------------------------------------------------------------------------
# Deribit application error handling
# ---------------------------------------------------------------------------


class TestDeribitErrorHandling:
    async def test_error_response_raises_value_error(self) -> None:
        """HTTP 200 with ``error`` key → ValueError with the error message."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(
                200, _jsonrpc_error(10009, "Invalid instrument")
            )
        )

        client = _client_with_mock_session(mock_session)
        with pytest.raises(ValueError, match="Invalid instrument"):
            await client.get_instruments("BTC")

    async def test_error_response_includes_error_code(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_error(13009, "Order not found"))
        )

        client = _client_with_mock_session(mock_session)
        with pytest.raises(ValueError, match="13009"):
            await client.get_ticker("INVALID-INSTRUMENT")

    async def test_error_response_no_retry(self) -> None:
        """Application-level errors are not retried."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, _jsonrpc_error(10009, "not found"))
        )

        client = _client_with_mock_session(mock_session)
        with pytest.raises(ValueError):
            await client.get_instruments("BTC")

        # Should only call once — application errors are not retried.
        assert mock_session.get.call_count == 1


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHttpErrorHandling:
    async def test_400_raised_immediately_no_retry(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(400))

        client = DeribitClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_instruments("BTC")

        assert mock_session.get.call_count == 1
        assert exc_info.value.response.status_code == 400

    async def test_429_raised_immediately_no_retry(self) -> None:
        """HTTP 429 must NOT be retried — gateway handles Retry-After."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(429))

        client = DeribitClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_instruments("BTC")

        assert mock_session.get.call_count == 1
        assert exc_info.value.response.status_code == 429

    async def test_404_raised_immediately_no_retry(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(404))

        client = DeribitClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_ticker("UNKNOWN")

        assert mock_session.get.call_count == 1

    async def test_5xx_retried_up_to_max_retries(self) -> None:
        """5xx errors are retried up to max_retries times."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(503))

        client = DeribitClient(session=mock_session, max_retries=3)

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_instruments("BTC")

        assert mock_session.get.call_count == 3

    async def test_5xx_succeeds_on_second_attempt(self) -> None:
        """Should succeed if 5xx is transient and recovers on retry."""
        good_response = _mock_response(200, _jsonrpc_ok([{"instrument_name": "BTC-PERPETUAL"}]))
        bad_response = _mock_response(503)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=[bad_response, good_response])

        client = DeribitClient(session=mock_session, max_retries=3)

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await client.get_instruments("BTC")

        assert len(result) == 1
        assert mock_session.get.call_count == 2

    async def test_5xx_with_max_retries_1_raises_after_one_call(self) -> None:
        """With max_retries=1, a single 5xx exhausts retries immediately."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(500))

        client = DeribitClient(session=mock_session, max_retries=1)

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_instruments("BTC")

        assert mock_session.get.call_count == 1


# ---------------------------------------------------------------------------
# Context manager and lifecycle
# ---------------------------------------------------------------------------


class TestContextManager:
    async def test_aenter_returns_client(self) -> None:
        client = DeribitClient()
        async with client as c:
            assert c is client

    async def test_aexit_closes_owned_session(self) -> None:
        """When the client owns the session, __aexit__ must close it."""
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = DeribitClient()
        client._session = mock_session
        client._owns_session = True

        await client.__aexit__(None, None, None)
        mock_session.aclose.assert_called_once()

    async def test_aexit_does_not_close_injected_session(self) -> None:
        """When an external session is injected, __aexit__ must NOT close it."""
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = DeribitClient(session=mock_session)
        await client.__aexit__(None, None, None)

        mock_session.aclose.assert_not_called()

    async def test_close_sets_session_to_none(self) -> None:
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = DeribitClient()
        client._session = mock_session
        client._owns_session = True

        await client.close()
        assert client._session is None

    async def test_close_noop_when_no_session(self) -> None:
        client = DeribitClient()
        # Should not raise.
        await client.close()


# ---------------------------------------------------------------------------
# Default and custom base URL
# ---------------------------------------------------------------------------


class TestBaseUrl:
    def test_default_base_url_contains_deribit(self) -> None:
        client = DeribitClient()
        assert "deribit.com" in client._base_url

    def test_default_base_url_has_api_v2(self) -> None:
        client = DeribitClient()
        assert "/api/v2" in client._base_url

    def test_custom_base_url(self) -> None:
        client = DeribitClient(base_url="https://test.deribit.com/api/v2")
        assert "test.deribit.com" in client._base_url

    def test_trailing_slash_stripped(self) -> None:
        client = DeribitClient(base_url="https://www.deribit.com/api/v2/")
        assert not client._base_url.endswith("/")

    def test_user_agent_set_in_headers(self) -> None:
        client = DeribitClient()
        headers = client._build_headers()
        assert "User-Agent" in headers
        assert "deribit" in headers["User-Agent"]

    def test_accept_json_header_set(self) -> None:
        client = DeribitClient()
        headers = client._build_headers()
        assert headers.get("Accept") == "application/json"
