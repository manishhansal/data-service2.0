"""
Delta Exchange OHLCV Normaliser — DS2-RCA-001 fix.

Validates and normalises raw Delta candle dicts (as returned by
``DeltaClient.get_candles``) into typed ``DeltaCandleRecord`` objects.

Key differences from BinanceOHLCVNormaliser
--------------------------------------------
* ``exchange`` is always ``"DELTA"``.
* Delta candle ``time`` is already converted to UTC epoch **milliseconds**
  by ``DeltaClient._normalise_candle()``.  No further conversion needed here.
* ``closeTime`` is also pre-computed by the client.
* The ``3m`` interval is **not** banned here (crypto exception).

Requirements: DS2-RCA-001, 13.1 (crypto 3m exception)
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from src.observability.logging import get_logger
from src.providers.adapters.delta_exchange import DELTA_INTERVALS

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical Pydantic v2 model
# ---------------------------------------------------------------------------


class DeltaCandleRecord(BaseModel):
    """Canonical representation of a single Delta Exchange OHLCV candle.

    Mirrors ``BinanceCandleRecord`` with ``exchange="DELTA"``.
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
    exchange: str = Field(default="DELTA")
    poor_quality: bool = Field(default=False)

    @field_validator("symbol")
    @classmethod
    def symbol_uppercase(cls, v: str) -> str:  # noqa: N805
        return v.upper()


# ---------------------------------------------------------------------------
# Normaliser
# ---------------------------------------------------------------------------


class DeltaOHLCVNormaliser:
    """Validates and normalises Delta candle dicts into ``DeltaCandleRecord``.

    Usage::

        normaliser = DeltaOHLCVNormaliser()
        records = normaliser.normalise_batch(raw_candles, "BTCUSD", "1h")

    The normaliser is stateless; create one instance and reuse it across
    requests.

    Note on ``3m`` interval
    ~~~~~~~~~~~~~~~~~~~~~~~
    The 3m restriction applies only to Indian equity/F&O data.  Delta
    natively supports ``3m`` and this normaliser accepts it.
    """

    def normalise(
        self,
        raw_candle: dict[str, Any],
        symbol: str,
        interval: str,
    ) -> Optional[DeltaCandleRecord]:
        """Validate and normalise a single Delta candle dict.

        Args:
            raw_candle: A canonical candle dict as returned by
                        ``DeltaClient.get_candles()`` — expected keys:
                        ``time``, ``open``, ``high``, ``low``, ``close``,
                        ``volume``, ``closeTime``.
            symbol:     Delta instrument symbol, e.g. ``"BTCUSD"``.
            interval:   Canonical interval string, e.g. ``"1h"``.

        Returns:
            A ``DeltaCandleRecord`` on success, ``None`` on validation failure.
        """
        # Step 1: interval must be a Delta-supported interval.
        if interval not in DELTA_INTERVALS:
            logger.warning(
                "delta_normaliser.invalid_interval",
                component="delta_normaliser",
                symbol=symbol,
                interval=interval,
            )
            return None

        # Step 2: extract + coerce fields.
        time_val = self._to_positive_int(raw_candle.get("time"))
        close_time_val = self._to_positive_int(raw_candle.get("closeTime"))
        open_val = self._to_positive_float(raw_candle.get("open"))
        high_val = self._to_positive_float(raw_candle.get("high"))
        low_val = self._to_positive_float(raw_candle.get("low"))
        close_val = self._to_positive_float(raw_candle.get("close"))
        volume_val = self._to_non_negative_float(raw_candle.get("volume"))

        # Step 3: required-field presence check.
        missing = [
            name for name, val in [
                ("time", time_val), ("closeTime", close_time_val),
                ("open", open_val), ("high", high_val),
                ("low", low_val), ("close", close_val), ("volume", volume_val),
            ]
            if val is None
        ]
        if missing:
            logger.warning(
                "delta_normaliser.missing_or_invalid_fields",
                component="delta_normaliser",
                symbol=symbol,
                interval=interval,
                missing_fields=missing,
            )
            return None

        # Step 4: OHLC invariants (same as Binance).
        assert time_val and close_time_val and open_val and high_val
        assert low_val is not None and close_val is not None and volume_val is not None

        failed: list[str] = []
        if high_val < max(open_val, close_val):
            failed.append(f"high({high_val}) < max(open,close)")
        if low_val > min(open_val, close_val):
            failed.append(f"low({low_val}) > min(open,close)")

        if failed:
            logger.warning(
                "delta_normaliser.ohlc_invariant_violation",
                component="delta_normaliser",
                symbol=symbol,
                interval=interval,
                open_time_ms=time_val,
                violations=failed,
            )
            return None

        # Step 5: build canonical record.
        return DeltaCandleRecord(
            symbol=symbol,
            interval=interval,
            time=time_val,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            volume=volume_val,
            closeTime=close_time_val,
            exchange="DELTA",
            poor_quality=False,
        )

    def normalise_batch(
        self,
        raw_candles: list[dict[str, Any]],
        symbol: str,
        interval: str,
    ) -> list[DeltaCandleRecord]:
        """Normalise a list of Delta candle dicts; silently drop invalid ones."""
        results: list[DeltaCandleRecord] = []
        rejected = 0
        for raw in raw_candles:
            rec = self.normalise(raw, symbol=symbol, interval=interval)
            if rec is not None:
                results.append(rec)
            else:
                rejected += 1
        if rejected:
            logger.info(
                "delta_normaliser.batch_complete",
                component="delta_normaliser",
                symbol=symbol,
                interval=interval,
                accepted=len(results),
                rejected=rejected,
            )
        return results

    # Private helpers — identical to BinanceOHLCVNormaliser
    @staticmethod
    def _to_positive_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _to_positive_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    @staticmethod
    def _to_non_negative_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None
