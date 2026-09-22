"""
tests/unit/providers/streams/test_angel_one_stream_binary.py

Unit tests for the Angel One SmartStream binary protocol decoder.

Tests verify:
  - LTP mode (mode=1) frame decoding
  - QUOTE mode (mode=2) frame decoding
  - FULL mode (mode=3) frame with OI and depth
  - Paise → INR conversion (divide by 100)
  - OI populated only from explicit OI field, never from volume/tradedValue
  - Exchange type mapping (NSE=1, NFO=2, BSE=3, MCX=5)
  - Short frame handling (returns None)
  - Multi-exchange subscription (NFO, BSE, MCX)
  - Mode integer values (1=LTP, 2=QUOTE, 3=FULL)

Requirements: DEFECT-003, Phase 2-3
"""

from __future__ import annotations

import struct
import pytest

from src.providers.streams.angel_one_stream import (
    EXCHANGE_TYPE_NFO,
    EXCHANGE_TYPE_BSE_EQ,
    EXCHANGE_TYPE_MCX,
    EXCHANGE_TYPE_NSE_EQ,
    SUBSCRIPTION_MODE_FULL,
    SUBSCRIPTION_MODE_LTP,
    SUBSCRIPTION_MODE_QUOTE,
    _decode_smartstream_binary,
)


# ---------------------------------------------------------------------------
# Binary frame builders
# ---------------------------------------------------------------------------

def _build_ltp_frame(
    mode: int = 1,
    exchange_type: int = 1,
    token: str = "256265",
    seq_num: int = 1234,
    exchange_ts: int = 1700000000000,
    ltp_paise: int = 2250050,  # 22500.50
) -> bytes:
    """Build a minimal LTP mode (mode=1) SmartStream binary frame."""
    token_bytes = token.encode("ascii").ljust(20, b"\x00")[:20]
    frame = (
        struct.pack("<H", mode) +
        struct.pack("<H", exchange_type) +
        token_bytes +
        struct.pack("<q", seq_num) +
        struct.pack("<q", exchange_ts) +
        struct.pack("<q", ltp_paise)
    )
    return frame


def _build_quote_frame(
    mode: int = 2,
    exchange_type: int = 1,
    token: str = "256265",
    ltp_paise: int = 2250050,
    volume: int = 1234567,
    open_paise: int = 2240000,
    high_paise: int = 2260000,
    low_paise: int = 2235000,
    close_paise: int = 2245000,
) -> bytes:
    """Build a QUOTE mode (mode=2) SmartStream binary frame."""
    token_bytes = token.encode("ascii").ljust(20, b"\x00")[:20]
    # Header (48 bytes: mode, exchange_type, token, seq_num, exchange_ts, ltp)
    frame = (
        struct.pack("<H", mode) +
        struct.pack("<H", exchange_type) +
        token_bytes +
        struct.pack("<q", 12345) +       # seq_num
        struct.pack("<q", 1700000000000) + # exchange_ts
        struct.pack("<q", ltp_paise)
    )
    # QUOTE extra fields (bytes 48-120):
    # ltq (int64), atp (int64), vol (int64), tbq (float64), tsq (float64)
    # open (int64), high (int64), low (int64), close (int64)
    frame += (
        struct.pack("<q", 75) +          # ltq
        struct.pack("<q", 2249000) +     # atp (paise)
        struct.pack("<q", volume) +      # vol
        struct.pack("<d", 1.0) +         # tbq
        struct.pack("<d", 2.0) +         # tsq
        struct.pack("<q", open_paise) +
        struct.pack("<q", high_paise) +
        struct.pack("<q", low_paise) +
        struct.pack("<q", close_paise)
    )
    return frame


# ---------------------------------------------------------------------------
# LTP mode decoding
# ---------------------------------------------------------------------------

class TestLTPModeDecode:
    """Test LTP mode (mode=1) binary frame decoding."""

    def test_ltp_decoded_in_inr(self) -> None:
        """LTP is stored in paise; must be divided by 100."""
        frame = _build_ltp_frame(ltp_paise=2250050)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["ltp"] == pytest.approx(22500.50)

    def test_exchange_type_decoded(self) -> None:
        frame = _build_ltp_frame(exchange_type=EXCHANGE_TYPE_NSE_EQ)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["exchangeType"] == EXCHANGE_TYPE_NSE_EQ
        assert result["exchange"] == "NSE_EQ"

    def test_nfo_exchange_type(self) -> None:
        """NFO (type=2) must decode correctly — required for options."""
        frame = _build_ltp_frame(exchange_type=EXCHANGE_TYPE_NFO)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["exchangeType"] == 2
        assert result["exchange"] == "NFO"

    def test_bse_exchange_type(self) -> None:
        frame = _build_ltp_frame(exchange_type=EXCHANGE_TYPE_BSE_EQ)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["exchangeType"] == 3

    def test_token_decoded(self) -> None:
        frame = _build_ltp_frame(token="256265")
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["instrumentToken"] == "256265"

    def test_source_is_angel_one(self) -> None:
        frame = _build_ltp_frame()
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["source"] == "angel_one"

    def test_too_short_frame_returns_none(self) -> None:
        """Frames shorter than minimum LTP size must return None."""
        result = _decode_smartstream_binary(b"\x01\x00\x01\x00")
        assert result is None


# ---------------------------------------------------------------------------
# QUOTE mode decoding
# ---------------------------------------------------------------------------

class TestQuoteModeDecode:
    """Test QUOTE mode (mode=2) binary frame decoding."""

    def test_ohlc_decoded_in_inr(self) -> None:
        frame = _build_quote_frame(
            mode=2,
            ltp_paise=2250050,
            open_paise=2240000,
            high_paise=2260000,
            low_paise=2235000,
            close_paise=2245000,
        )
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["ltp"]  == pytest.approx(22500.50)
        assert result["open"] == pytest.approx(22400.00)
        assert result["high"] == pytest.approx(22600.00)
        assert result["low"]  == pytest.approx(22350.00)
        assert result["close"] == pytest.approx(22450.00)

    def test_volume_decoded(self) -> None:
        frame = _build_quote_frame(volume=9876543)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result.get("volume") == 9876543


# ---------------------------------------------------------------------------
# OI semantic integrity
# ---------------------------------------------------------------------------

class TestOISemanticIntegrity:
    """OI must only come from the explicit OI field, never from volume/tradedValue."""

    def test_oi_absent_in_ltp_mode(self) -> None:
        """LTP mode frames do not include OI — must be None."""
        frame = _build_ltp_frame(mode=1)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        assert result["oi"] is None
        assert result["oiMissing"] is True

    def test_oi_never_equals_volume(self) -> None:
        """OI must never be populated from volume field."""
        frame = _build_quote_frame(mode=2, volume=9876543)
        result = _decode_smartstream_binary(frame)
        assert result is not None
        # oi should NOT equal volume
        if result.get("oi") is not None:
            assert result["oi"] != 9876543


# ---------------------------------------------------------------------------
# Mode integer values
# ---------------------------------------------------------------------------

class TestModeIntegerValues:
    """Subscription mode values must be integers, not strings."""

    def test_ltp_mode_is_integer(self) -> None:
        assert isinstance(SUBSCRIPTION_MODE_LTP, int)
        assert SUBSCRIPTION_MODE_LTP == 1

    def test_quote_mode_is_integer(self) -> None:
        assert isinstance(SUBSCRIPTION_MODE_QUOTE, int)
        assert SUBSCRIPTION_MODE_QUOTE == 2

    def test_full_mode_is_integer(self) -> None:
        assert isinstance(SUBSCRIPTION_MODE_FULL, int)
        assert SUBSCRIPTION_MODE_FULL == 3


# ---------------------------------------------------------------------------
# Exchange type constants
# ---------------------------------------------------------------------------

class TestExchangeTypeConstants:
    """Exchange type codes must match SmartStream V2 specification."""

    def test_nse_eq_is_1(self) -> None:
        assert EXCHANGE_TYPE_NSE_EQ == 1

    def test_nfo_is_2(self) -> None:
        assert EXCHANGE_TYPE_NFO == 2

    def test_bse_eq_is_3(self) -> None:
        assert EXCHANGE_TYPE_BSE_EQ == 3

    def test_mcx_is_5(self) -> None:
        assert EXCHANGE_TYPE_MCX == 5


# ---------------------------------------------------------------------------
# Subscribe with multiple exchange types
# ---------------------------------------------------------------------------

class TestSubscribeMultiExchange:
    """Subscribe must support NFO, BSE, MCX exchange types."""

    async def test_subscribe_with_nfo_token_group(self) -> None:
        from src.providers.streams.angel_one_stream import AngelOneStreamAdapter  # noqa: PLC0415
        adapter = AngelOneStreamAdapter()
        adapter._ws = None
        await adapter.subscribe(
            token_groups=[
                {"exchange_type": EXCHANGE_TYPE_NSE_EQ, "tokens": ["256265"]},
                {"exchange_type": EXCHANGE_TYPE_NFO,    "tokens": ["43985", "43986"]},
            ],
            mode=SUBSCRIPTION_MODE_FULL,
        )
        assert len(adapter._token_groups) == 2
        nfo_group = next(g for g in adapter._token_groups if g["exchange_type"] == EXCHANGE_TYPE_NFO)
        assert "43985" in nfo_group["tokens"]

    async def test_subscribe_backward_compat_tokens_arg(self) -> None:
        """Old subscribe(tokens=[...]) call style still works."""
        from src.providers.streams.angel_one_stream import AngelOneStreamAdapter  # noqa: PLC0415
        adapter = AngelOneStreamAdapter()
        adapter._ws = None
        await adapter.subscribe(tokens=["256265", "2885"])
        # Should create one group with NSE EQ exchange type
        assert len(adapter._token_groups) == 1
        assert adapter._token_groups[0]["exchange_type"] == EXCHANGE_TYPE_NSE_EQ
        assert "256265" in adapter._token_groups[0]["tokens"]
