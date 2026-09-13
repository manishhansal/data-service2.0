"""
Unit tests for src/providers/adapters/scrapling_nse.py

Tests cover:
- Successful live quote fetch returns dict with expected fields
- HTTP 403 raises ProviderAuthError
- HTTP 429 raises ProviderRateLimitedError (with and without Retry-After)
- HTTP 503 raises ProviderUnavailableError
- Connection error raises ProviderUnavailableError
- Empty / closed-market response raises ProviderMarketClosedError
- Successful option chain fetch returns dict with expected structure
- Option chain empty payload raises ProviderMarketClosedError
- Successful instrument master fetch merges equity + index lists
- Instrument master partial failure still returns available instruments
- sourceType classification on all successful responses
- _looks_like_json utility helper

Requirements: 5.9, 22.1
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.schemas.provider import SourceType
from src.providers.adapters.base import (
    ProviderAuthError,
    ProviderDataError,
    ProviderMarketClosedError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from src.providers.adapters.scrapling_nse import (
    ScraplingNseAdapter,
    _PROVIDER_NAME,
    _looks_like_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    body: Any = None,
    content_type: str = "application/json",
    retry_after: str | None = None,
) -> MagicMock:
    """Build a minimal mock that mimics a curl_cffi ``Response``."""
    resp = MagicMock()
    resp.status_code = status_code

    raw_content = (
        json.dumps(body).encode() if body is not None else b""
    )
    resp.content = raw_content

    headers: dict[str, str] = {"content-type": content_type}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    resp.headers = headers

    return resp


def _make_live_quote_payload(symbol: str = "NIFTY") -> dict[str, Any]:
    """Minimal NSE quote JSON that passes the ``priceInfo`` guard."""
    return {
        "info": {"symbol": symbol, "companyName": "Nifty 50"},
        "metadata": {"instrumentType": "Index"},
        "priceInfo": {
            "lastPrice": 22150.50,
            "open": 22100.00,
            "high": 22200.00,
            "low": 22050.00,
            "change": 45.25,
            "pChange": 0.20,
            "previousClose": 22105.25,
        },
        "securityWiseDP": {
            "upperBand": 24315.75,
            "lowerBand": 19894.75,
        },
    }


def _make_option_chain_payload(underlying: str = "NIFTY") -> dict[str, Any]:
    """Minimal NSE option chain JSON with non-empty ``filtered.data``."""
    return {
        "records": {
            "expiryDates": ["25-Jan-2024", "01-Feb-2024"],
            "strikePrices": [22000, 22100, 22200],
            "data": [
                {
                    "strikePrice": 22000,
                    "expiryDate": "25-Jan-2024",
                    "CE": {"openInterest": 5000, "changeinOpenInterest": 100},
                    "PE": {"openInterest": 3000, "changeinOpenInterest": -50},
                }
            ],
            "underlying": underlying,
            "underlyingValue": 22150.5,
        },
        "filtered": {
            "data": [
                {
                    "strikePrice": 22000,
                    "expiryDate": "25-Jan-2024",
                    "CE": {"openInterest": 5000},
                    "PE": {"openInterest": 3000},
                }
            ],
            "PE": {"totOI": 3000, "totVol": 2000},
            "CE": {"totOI": 5000, "totVol": 3500},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def adapter() -> ScraplingNseAdapter:
    """Return an adapter with throttling effectively disabled for tests."""
    return ScraplingNseAdapter(
        requests_per_second=1000.0,  # remove rate limiting in unit tests
        connect_timeout=5.0,
        read_timeout=5.0,
        max_retries=0,  # no retries — tests verify single-attempt behaviour
    )


# ---------------------------------------------------------------------------
# Tests: fetch_live_quote
# ---------------------------------------------------------------------------


class TestFetchLiveQuote:
    """Tests for ScraplingNseAdapter.fetch_live_quote."""

    @pytest.mark.asyncio
    async def test_successful_equity_quote_returns_expected_fields(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """A 200 response with a valid payload returns a dict containing
        priceInfo, securityWiseDP, _sourceType, and _provider."""
        payload = _make_live_quote_payload("RELIANCE")
        mock_resp = _make_response(body=payload)

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await adapter.fetch_live_quote("RELIANCE", exchange="NSE")

        assert result["priceInfo"]["lastPrice"] == 22150.50
        assert result["_sourceType"] == SourceType.OPEN_SOURCE_NSE_DERIVED.value
        assert result["_provider"] == _PROVIDER_NAME

    @pytest.mark.asyncio
    async def test_successful_quote_source_type_is_open_source_nse_derived(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """_sourceType must be OPEN_SOURCE_NSE_DERIVED on every successful response."""
        payload = _make_live_quote_payload("NIFTY")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await adapter.fetch_live_quote("NIFTY")

        assert result["_sourceType"] == SourceType.OPEN_SOURCE_NSE_DERIVED.value

    @pytest.mark.asyncio
    async def test_empty_price_info_raises_market_closed(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """A dict without priceInfo signals a closed market; expect
        ProviderMarketClosedError (not a provider failure)."""
        empty_payload: dict[str, Any] = {"info": {}}

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = empty_payload

            with pytest.raises(ProviderMarketClosedError) as exc_info:
                await adapter.fetch_live_quote("NIFTY")

        assert exc_info.value.provider == _PROVIDER_NAME

    @pytest.mark.asyncio
    async def test_empty_dict_response_raises_market_closed(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """An empty dict response also signals market closed."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {}

            with pytest.raises(ProviderMarketClosedError):
                await adapter.fetch_live_quote("NIFTY")

    @pytest.mark.asyncio
    async def test_http_403_raises_provider_auth_error(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """HTTP 403 from NSE (WAF block) must raise ProviderAuthError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderAuthError(
                "NSE returned HTTP 403",
                provider=_PROVIDER_NAME,
                status_code=403,
            )

            with pytest.raises(ProviderAuthError) as exc_info:
                await adapter.fetch_live_quote("NIFTY")

        assert exc_info.value.status_code == 403
        assert exc_info.value.provider == _PROVIDER_NAME

    @pytest.mark.asyncio
    async def test_http_429_raises_provider_rate_limited_error(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """HTTP 429 must raise ProviderRateLimitedError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderRateLimitedError(
                "NSE returned HTTP 429",
                provider=_PROVIDER_NAME,
                status_code=429,
                retry_after_s=60,
            )

            with pytest.raises(ProviderRateLimitedError) as exc_info:
                await adapter.fetch_live_quote("NIFTY")

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_s == 60

    @pytest.mark.asyncio
    async def test_http_503_raises_provider_unavailable_error(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """HTTP 503 must raise ProviderUnavailableError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderUnavailableError(
                "NSE returned HTTP 503",
                provider=_PROVIDER_NAME,
                status_code=503,
            )

            with pytest.raises(ProviderUnavailableError) as exc_info:
                await adapter.fetch_live_quote("NIFTY")

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_connection_error_raises_provider_unavailable_error(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """A network-level connection error must surface as ProviderUnavailableError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderUnavailableError(
                "Connection error fetching NSE: Connection refused",
                provider=_PROVIDER_NAME,
                status_code=None,
            )

            with pytest.raises(ProviderUnavailableError) as exc_info:
                await adapter.fetch_live_quote("NIFTY")

        assert exc_info.value.status_code is None

    @pytest.mark.asyncio
    async def test_nfo_exchange_fetches_derivative_endpoint(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """NFO exchange routes to the derivative quote endpoint."""
        payload = _make_live_quote_payload("NIFTY25JANFUT")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await adapter.fetch_live_quote("NIFTY25JANFUT", exchange="NFO")

        # Verify _get was called with the derivative URL
        call_url = mock_get.call_args[0][0]
        assert "quote-derivative" in call_url

    @pytest.mark.asyncio
    async def test_nse_exchange_fetches_equity_endpoint(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """NSE exchange routes to the equity quote endpoint."""
        payload = _make_live_quote_payload("RELIANCE")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            await adapter.fetch_live_quote("RELIANCE", exchange="NSE")

        call_url = mock_get.call_args[0][0]
        assert "quote-equity" in call_url


# ---------------------------------------------------------------------------
# Tests: fetch_option_chain
# ---------------------------------------------------------------------------


class TestFetchOptionChain:
    """Tests for ScraplingNseAdapter.fetch_option_chain."""

    @pytest.mark.asyncio
    async def test_successful_index_option_chain(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Index underlyings (NIFTY) return a dict with filtered.data and
        _sourceType / _provider metadata."""
        payload = _make_option_chain_payload("NIFTY")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await adapter.fetch_option_chain("NIFTY")

        assert "filtered" in result
        assert len(result["filtered"]["data"]) > 0
        assert result["_sourceType"] == SourceType.OPEN_SOURCE_NSE_DERIVED.value
        assert result["_provider"] == _PROVIDER_NAME

    @pytest.mark.asyncio
    async def test_option_chain_with_expiry_appends_metadata(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """When expiry is passed, ``_requestedExpiry`` is included in the result."""
        payload = _make_option_chain_payload("BANKNIFTY")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            result = await adapter.fetch_option_chain("BANKNIFTY", expiry="2024-01-25")

        assert result["_requestedExpiry"] == "2024-01-25"

    @pytest.mark.asyncio
    async def test_empty_option_chain_raises_market_closed(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Empty filtered.data signals market closed or no contracts listed."""
        empty_payload: dict[str, Any] = {"filtered": {"data": []}}

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = empty_payload

            with pytest.raises(ProviderMarketClosedError):
                await adapter.fetch_option_chain("NIFTY")

    @pytest.mark.asyncio
    async def test_missing_filtered_key_raises_market_closed(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """A payload with no 'filtered' key raises ProviderMarketClosedError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {}

            with pytest.raises(ProviderMarketClosedError):
                await adapter.fetch_option_chain("NIFTY")

    @pytest.mark.asyncio
    async def test_index_underlying_uses_indices_endpoint(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Known index underlyings route to the option-chain-indices endpoint."""
        payload = _make_option_chain_payload("NIFTY")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            await adapter.fetch_option_chain("NIFTY")

        call_url = mock_get.call_args[0][0]
        assert "option-chain-indices" in call_url

    @pytest.mark.asyncio
    async def test_equity_underlying_uses_equities_endpoint(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Non-index underlyings route to the option-chain-equities endpoint."""
        payload = _make_option_chain_payload("RELIANCE")

        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = payload

            await adapter.fetch_option_chain("RELIANCE")

        call_url = mock_get.call_args[0][0]
        assert "option-chain-equities" in call_url

    @pytest.mark.asyncio
    async def test_http_403_propagates(self, adapter: ScraplingNseAdapter) -> None:
        """HTTP 403 from the option chain endpoint propagates as ProviderAuthError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderAuthError(
                "HTTP 403", provider=_PROVIDER_NAME, status_code=403
            )

            with pytest.raises(ProviderAuthError):
                await adapter.fetch_option_chain("NIFTY")

    @pytest.mark.asyncio
    async def test_http_429_propagates(self, adapter: ScraplingNseAdapter) -> None:
        """HTTP 429 propagates as ProviderRateLimitedError."""
        with patch.object(adapter, "_get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = ProviderRateLimitedError(
                "HTTP 429", provider=_PROVIDER_NAME, status_code=429
            )

            with pytest.raises(ProviderRateLimitedError):
                await adapter.fetch_option_chain("NIFTY")


# ---------------------------------------------------------------------------
# Tests: fetch_instrument_master
# ---------------------------------------------------------------------------


class TestFetchInstrumentMaster:
    """Tests for ScraplingNseAdapter.fetch_instrument_master."""

    @pytest.mark.asyncio
    async def test_successful_fetch_merges_equity_and_index(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """A successful fetch returns instruments from both equity and index
        endpoints merged into a single flat list."""
        eq_payload = {
            "data": [
                {"symbol": "RELIANCE", "isin": "INE002A01018", "series": "EQ"},
                {"symbol": "TCS", "isin": "INE467B01029", "series": "EQ"},
            ]
        }
        idx_payload = {
            "indexDetailsList": [
                {"indexSymbol": "NIFTY 50", "open": "22100.00", "high": "22200.00"},
            ]
        }

        call_count = 0

        async def mock_get(url: str, **_: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if "master-quote" in url:
                return eq_payload
            if "allIndices" in url:
                return idx_payload
            return {}

        with patch.object(adapter, "_get", side_effect=mock_get):
            result = await adapter.fetch_instrument_master()

        assert len(result) == 3  # 2 equities + 1 index
        symbols = [r.get("symbol") or r.get("indexSymbol") for r in result]
        assert "RELIANCE" in symbols or any("RELIANCE" in str(r) for r in result)

    @pytest.mark.asyncio
    async def test_all_instruments_have_source_type_metadata(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Every returned instrument must have _sourceType and _provider set."""
        eq_payload = {"data": [{"symbol": "INFY"}, {"symbol": "WIPRO"}]}
        idx_payload = {"indexDetailsList": [{"indexSymbol": "NIFTY 50"}]}

        async def mock_get(url: str, **_: Any) -> dict[str, Any]:
            if "master-quote" in url:
                return eq_payload
            return idx_payload

        with patch.object(adapter, "_get", side_effect=mock_get):
            result = await adapter.fetch_instrument_master()

        for instrument in result:
            assert instrument["_sourceType"] == SourceType.OPEN_SOURCE_NSE_DERIVED.value
            assert instrument["_provider"] == _PROVIDER_NAME

    @pytest.mark.asyncio
    async def test_equity_endpoint_failure_returns_index_instruments(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """If the equity endpoint fails, the index instruments are still returned."""
        idx_payload = {"indexDetailsList": [{"indexSymbol": "NIFTY 50"}]}

        async def mock_get(url: str, **_: Any) -> dict[str, Any]:
            if "master-quote" in url:
                raise ProviderUnavailableError("equity master down", provider=_PROVIDER_NAME)
            return idx_payload

        with patch.object(adapter, "_get", side_effect=mock_get):
            result = await adapter.fetch_instrument_master()

        assert len(result) == 1
        assert result[0]["_instrumentCategory"] == "IDX"

    @pytest.mark.asyncio
    async def test_both_endpoints_fail_raises_provider_data_error(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """If both endpoints fail, ProviderDataError is raised."""
        async def mock_get(url: str, **_: Any) -> dict[str, Any]:
            raise ProviderUnavailableError("down", provider=_PROVIDER_NAME)

        with patch.object(adapter, "_get", side_effect=mock_get):
            with pytest.raises(ProviderDataError) as exc_info:
                await adapter.fetch_instrument_master()

        assert "no instruments" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_list_response_is_handled(
        self, adapter: ScraplingNseAdapter
    ) -> None:
        """Some NSE endpoints return a bare list rather than a wrapped dict."""
        list_payload = [{"symbol": "HDFCBANK"}, {"symbol": "ICICIBANK"}]
        idx_payload: dict[str, Any] = {}  # will fail gracefully

        async def mock_get(url: str, **_: Any) -> Any:
            if "master-quote" in url:
                return list_payload
            return idx_payload

        with patch.object(adapter, "_get", side_effect=mock_get):
            result = await adapter.fetch_instrument_master()

        eq_instruments = [r for r in result if r.get("_instrumentCategory") == "EQ"]
        assert len(eq_instruments) == 2


# ---------------------------------------------------------------------------
# Tests: source type classification
# ---------------------------------------------------------------------------


class TestSourceTypeClassification:
    """Verify the SOURCE_TYPE class constant matches the expected enum value."""

    def test_source_type_is_open_source_nse_derived(self) -> None:
        """ScraplingNseAdapter.SOURCE_TYPE must be OPEN_SOURCE_NSE_DERIVED."""
        assert (
            ScraplingNseAdapter.SOURCE_TYPE
            is SourceType.OPEN_SOURCE_NSE_DERIVED
        )

    def test_source_type_value_string(self) -> None:
        """The string value must match the canonical constant."""
        assert ScraplingNseAdapter.SOURCE_TYPE.value == "OPEN_SOURCE_NSE_DERIVED"


# ---------------------------------------------------------------------------
# Tests: _looks_like_json helper
# ---------------------------------------------------------------------------


class TestLooksLikeJson:
    """Unit tests for the _looks_like_json utility."""

    def test_object_bytes_returns_true(self) -> None:
        assert _looks_like_json(b'{"key": "value"}') is True

    def test_array_bytes_returns_true(self) -> None:
        assert _looks_like_json(b'[{"a": 1}]') is True

    def test_html_returns_false(self) -> None:
        assert _looks_like_json(b"<html><body>hello</body></html>") is False

    def test_empty_bytes_returns_false(self) -> None:
        assert _looks_like_json(b"") is False

    def test_whitespace_prefixed_object_returns_true(self) -> None:
        assert _looks_like_json(b"  \n  {  }") is True

    def test_plain_string_returns_false(self) -> None:
        assert _looks_like_json(b"hello world") is False


# ---------------------------------------------------------------------------
# Tests: _get — direct HTTP error mapping via session mock
# ---------------------------------------------------------------------------


class TestGetErrorMapping:
    """Tests that exercise _get() directly to verify HTTP status → exception mapping."""

    @pytest.fixture()
    def adapter_with_seed(self) -> ScraplingNseAdapter:
        a = ScraplingNseAdapter(requests_per_second=1000.0, max_retries=0)
        a._session_seeded = True  # skip homepage seeding
        return a

    @pytest.mark.asyncio
    async def test_403_raises_provider_auth_error(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_make_response(status_code=403, content_type="text/html")
        )
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderAuthError) as exc_info:
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_429_raises_provider_rate_limited_error(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_make_response(
                status_code=429, content_type="text/html", retry_after="30"
            )
        )
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_s == 30

    @pytest.mark.asyncio
    async def test_429_without_retry_after_sets_none(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        """When no Retry-After header is present, retry_after_s must be None."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_make_response(status_code=429, content_type="text/html")
        )
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

        assert exc_info.value.retry_after_s is None

    @pytest.mark.asyncio
    async def test_503_raises_provider_unavailable_error(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_make_response(status_code=503, content_type="text/html")
        )
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderUnavailableError) as exc_info:
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_connection_error_raises_provider_unavailable(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=ConnectionError("Connection refused"))
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderUnavailableError):
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

    @pytest.mark.asyncio
    async def test_empty_body_raises_provider_data_error(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        """An HTTP 200 with an empty body raises ProviderDataError."""
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b""
        resp.headers = {"content-type": "application/json"}

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=resp)
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderDataError):
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

    @pytest.mark.asyncio
    async def test_invalid_json_raises_provider_data_error(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        """An HTTP 200 with non-JSON body raises ProviderDataError."""
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"not json {{{{"
        resp.headers = {"content-type": "application/json"}

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=resp)
        adapter_with_seed._session = mock_session

        with pytest.raises(ProviderDataError):
            await adapter_with_seed._get("https://www.nseindia.com/api/quote-equity")

    @pytest.mark.asyncio
    async def test_valid_json_200_returns_parsed_dict(
        self, adapter_with_seed: ScraplingNseAdapter
    ) -> None:
        """A valid JSON 200 response returns the parsed dict."""
        payload = {"priceInfo": {"lastPrice": 22000.0}}
        resp = _make_response(body=payload)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=resp)
        adapter_with_seed._session = mock_session

        result = await adapter_with_seed._get(
            "https://www.nseindia.com/api/quote-equity"
        )

        assert result["priceInfo"]["lastPrice"] == 22000.0


# ---------------------------------------------------------------------------
# Tests: base error class hierarchy
# ---------------------------------------------------------------------------


class TestBaseErrorClasses:
    """Verify the exception hierarchy from base.py."""

    def test_all_errors_inherit_from_provider_error(self) -> None:
        from src.providers.adapters.base import (
            ProviderError,
            ProviderUnavailableError,
            ProviderRateLimitedError,
            ProviderAuthError,
            ProviderDataError,
            ProviderMarketClosedError,
            ProviderUnsupportedError,
        )
        for cls in (
            ProviderUnavailableError,
            ProviderRateLimitedError,
            ProviderAuthError,
            ProviderDataError,
            ProviderMarketClosedError,
            ProviderUnsupportedError,
        ):
            assert issubclass(cls, ProviderError), f"{cls} must subclass ProviderError"

    def test_provider_error_stores_fields(self) -> None:
        from src.providers.adapters.base import ProviderError
        err = ProviderError(
            "test error",
            provider="scrapling_nse",
            status_code=503,
            retry_after_s=30,
        )
        assert err.message == "test error"
        assert err.provider == "scrapling_nse"
        assert err.status_code == 503
        assert err.retry_after_s == 30

    def test_provider_rate_limited_stores_retry_after(self) -> None:
        from src.providers.adapters.base import ProviderRateLimitedError
        err = ProviderRateLimitedError(
            "rate limited",
            provider="scrapling_nse",
            status_code=429,
            retry_after_s=60,
        )
        assert err.retry_after_s == 60

    def test_str_representation(self) -> None:
        from src.providers.adapters.base import ProviderUnavailableError
        err = ProviderUnavailableError("NSE is down")
        assert "NSE is down" in str(err)
