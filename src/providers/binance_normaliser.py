"""
Binance OHLCV Normaliser — Task 11.2.

Validates and normalises raw Binance kline dicts (as returned by
``BinanceClient.get_klines``) into typed ``BinanceCandleRecord`` objects
before they reach the persistence layer.

Key design decisions
--------------------
* ``3m`` interval is **explicitly allowed** here.  The 3m ban applies only
  to Indian market data.  Binance natively supports the 3m candle interval
  for crypto (Requirements 13.1, design: "3m Interval Rule" exception).
* On validation failure the ``normalise`` method returns ``None`` — invalid
  candles are silently skipped rather than raising, so a single bad candle
  from a batch does not abort the whole response.
* ``BinanceCandleRecord`` is a Pydantic v2 ``BaseModel`` with strict field
  types.  No provider-specific type is exposed beyond this module.
* Validation matches the OHLCV invariants from Requirement 13.8:
    high >= max(open, close)
    low  <= min(open, close)
    all prices > 0
    volume >= 0
    time and closeTime must be positive integers

Requirements: 13.1, 13.8, 13.10
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from src.observability.logging import get_logger
from src.providers.adapters.binance_rest import BINANCE_INTERVALS

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical Pydantic v2 model
# ---------------------------------------------------------------------------


class BinanceCandleRecord(BaseModel):
    """Canonical representation of a single Binance OHLCV candle.

    Fields
    ------
    symbol      : Binance trading symbol, e.g. ``"BTCUSDT"``.
    interval    : Kline interval, e.g. ``"1h"``, ``"3m"``.
    time        : Candle open time as UTC epoch milliseconds (openTime).
    open        : Opening price.
    high        : Highest price in the interval.
    low         : Lowest price in the interval.
    close       : Closing price.
    volume      : Base-asset traded volume (non-negative).
    closeTime   : Candle close time as UTC epoch milliseconds.
    exchange    : Always ``"BINANCE"`` for records produced by this normaliser.
    poor_quality: ``True`` when the record passed validation but is flagged
                  by a downstream quality stage.  Defaults to ``False`` at
                  normalisation time.
    """

    symbol: str = Field(..., min_length=1)
    interval: str = Field(..., min_length=1)
    time: int = Field(..., gt=0, description="openTime — UTC epoch ms")
    open: float = Field(..., gt=0)
    high: float = Field(..., gt=0)
    low: float = Field(..., gt=0)
    close: float = Field(..., gt=0)
    volume: float = Field(..., ge=0)
    closeTime: int = Field(..., gt=0, description="closeTime — UTC epoch ms")
    exchange: str = Field(default="BINANCE")
    poor_quality: bool = Field(default=False)

    @field_validator("symbol")
    @classmethod
    def symbol_uppercase(cls, v: str) -> str:
        return v.upper()


# ---------------------------------------------------------------------------
# Normaliser
# ---------------------------------------------------------------------------


class BinanceOHLCVNormaliser:
    """Validates and normalises raw Binance kline dicts.

    The normaliser is stateless; create a single instance and reuse it.

    Usage::

        normaliser = BinanceOHLCVNormaliser()

        # Single candle (returns None on failure)
        record = normaliser.normalise(raw_candle, symbol="BTCUSDT", interval="1h")

        # Batch (skips invalid candles silently)
        records = normaliser.normalise_batch(raw_candles, symbol="ETHUSDT", interval="4h")

    Notes on ``3m`` interval
    ~~~~~~~~~~~~~~~~~~~~~~~~
    The ``3m`` interval restriction applies **only** to Indian market data.
    Binance natively supports ``3m`` candles and this normaliser accepts it
    without error (Requirement 13.1).

    Requirements: 13.1, 13.8, 13.10
    """

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def normalise(
        self,
        raw_candle: dict[str, Any],
        symbol: str,
        interval: str,
    ) -> Optional[BinanceCandleRecord]:
        """Validate and normalise a single raw Binance kline dict.

        Args:
            raw_candle: A single normalised kline dict as returned by
                        ``BinanceClient.get_klines`` — expected keys:
                        ``time``, ``open``, ``high``, ``low``, ``close``,
                        ``volume``, ``closeTime``.
            symbol:     Binance trading symbol, e.g. ``"BTCUSDT"``.
            interval:   Kline interval string, e.g. ``"1h"``.

        Returns:
            A ``BinanceCandleRecord`` on success, ``None`` on any validation
            failure.  The caller should skip ``None`` results.

        Notes
        -----
        ``3m`` is **not** rejected here — it is a valid Binance crypto
        interval.  The 3m ban is scoped to Indian market data only.
        """
        # ── Step 1: interval must be a Binance-supported interval ──────────
        if interval not in BINANCE_INTERVALS:
            logger.warning(
                "binance_normaliser.invalid_interval",
                component="binance_normaliser",
                symbol=symbol,
                interval=interval,
                reason="interval not in BINANCE_INTERVALS",
            )
            return None

        # ── Step 2: extract and coerce raw values ──────────────────────────
        time_val = self._to_positive_int(raw_candle.get("time"))
        close_time_val = self._to_positive_int(raw_candle.get("closeTime"))
        open_val = self._to_positive_float(raw_candle.get("open"))
        high_val = self._to_positive_float(raw_candle.get("high"))
        low_val = self._to_positive_float(raw_candle.get("low"))
        close_val = self._to_positive_float(raw_candle.get("close"))
        volume_val = self._to_non_negative_float(raw_candle.get("volume"))

        # ── Step 3: required-field presence check ─────────────────────────
        missing: list[str] = []
        if time_val is None:
            missing.append("time")
        if close_time_val is None:
            missing.append("closeTime")
        if open_val is None:
            missing.append("open")
        if high_val is None:
            missing.append("high")
        if low_val is None:
            missing.append("low")
        if close_val is None:
            missing.append("close")
        if volume_val is None:
            missing.append("volume")

        if missing:
            logger.warning(
                "binance_normaliser.missing_or_invalid_fields",
                component="binance_normaliser",
                symbol=symbol,
                interval=interval,
                missing_fields=missing,
                raw_time=raw_candle.get("time"),
            )
            return None

        # All values confirmed non-None; narrow types for the invariant checks
        assert time_val is not None
        assert close_time_val is not None
        assert open_val is not None
        assert high_val is not None
        assert low_val is not None
        assert close_val is not None
        assert volume_val is not None

        # ── Step 4: OHLC invariant checks (Requirement 13.8) ──────────────
        failed: list[str] = []

        if high_val < max(open_val, close_val):
            failed.append(f"high({high_val}) < max(open({open_val}), close({close_val}))")

        if low_val > min(open_val, close_val):
            failed.append(f"low({low_val}) > min(open({open_val}), close({close_val}))")

        if failed:
            logger.warning(
                "binance_normaliser.ohlc_invariant_violation",
                component="binance_normaliser",
                symbol=symbol,
                interval=interval,
                open_time_ms=time_val,
                violations=failed,
            )
            return None

        # ── Step 5: build and return the canonical record ─────────────────
        return BinanceCandleRecord(
            symbol=symbol,
            interval=interval,
            time=time_val,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            volume=volume_val,
            closeTime=close_time_val,
            exchange="BINANCE",
            poor_quality=False,
        )

    def normalise_batch(
        self,
        raw_candles: list[dict[str, Any]],
        symbol: str,
        interval: str,
    ) -> list[BinanceCandleRecord]:
        """Normalise a list of raw Binance kline dicts, skipping invalid ones.

        Args:
            raw_candles: List of raw kline dicts from ``BinanceClient.get_klines``.
            symbol:      Binance trading symbol.
            interval:    Kline interval string.

        Returns:
            List of valid ``BinanceCandleRecord`` objects.  Any candle that
            fails validation is silently dropped (requirement 13.10).
        """
        results: list[BinanceCandleRecord] = []
        rejected = 0

        for raw in raw_candles:
            record = self.normalise(raw, symbol=symbol, interval=interval)
            if record is not None:
                results.append(record)
            else:
                rejected += 1

        if rejected:
            logger.info(
                "binance_normaliser.batch_complete",
                component="binance_normaliser",
                symbol=symbol,
                interval=interval,
                accepted=len(results),
                rejected=rejected,
            )

        return results

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_positive_int(value: Any) -> Optional[int]:
        """Convert *value* to a positive integer, returning None on failure."""
        if value is None:
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _to_positive_float(value: Any) -> Optional[float]:
        """Convert *value* to a strictly positive float, None on failure."""
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _to_non_negative_float(value: Any) -> Optional[float]:
        """Convert *value* to a non-negative float (≥ 0), None on failure."""
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None
