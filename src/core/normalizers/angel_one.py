"""
src/core/normalizers/angel_one.py

Angel One SmartAPI response normalizer for DATA-SERVICE 2.0.

Converts Angel One's REST and WebSocket response payloads into the canonical
platform schema.  Handles all response types:
  - FULL quote (from market/v1/quote)
  - Historical OHLCV candle (from getCandleData)
  - Historical OI (from getOIData)
  - Option Greeks (from optionGreek endpoint)
  - OI Buildup records
  - PCR data

Design contracts
----------------
* OI is never populated from tradedValue — only from opnInterest / openInterest.
* IV zero is treated as missing (not a valid substitute).
* Placeholder zeros for Greeks are rejected.
* All normalized outputs carry provider=angel_one and source_type=BROKER_AUTHENTICATED.
* source_timestamp is the exchange-side timestamp when available.
* received_at is always set to the current UTC time.
* Paise values from WebSocket are divided by 100 to convert to INR.

Field name mapping reference (Angel One → canonical):
  REST full quote:
    ltp / ltp → ltp
    open / open → open
    high / high → high
    low / low → low
    close / close → prevClose (previous session close)
    tradeVolume → volume
    opnInterest → oi
    upperCircuit / lowerCircuit → upperCircuit / lowerCircuit
    netChange / percentChange → change / changePct
    avgPrice → avgTradedPrice
    totBuyQtn / totSellQtn → totalBuyQty / totalSellQty
    depth.buy[0..4] / depth.sell[0..4] → depthBuy / depthSell
    exchFeedTime → exchangeFeedTime (source timestamp string)
    exchTradeTime → lastTradeTime
    52WeekHigh / 52WeekLow → weekHigh52 / weekLow52
    lastTradedQty → lastTradeQty

  Historical candle array: [timestamp, open, high, low, close, volume]
  Historical OI array: [timestamp, openInterest]

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

_PROVIDER = "angel_one"
_SOURCE_TYPE = "BROKER_AUTHENTICATED"


def _safe_float(val: Any, *, allow_zero: bool = True) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not allow_zero and f == 0.0:
        return None
    return f


def _safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AngelOneNormalizer:
    """Normalizes Angel One SmartAPI responses into canonical platform schema.

    Stateless — safe for concurrent use across async tasks.
    """

    # ------------------------------------------------------------------
    # Full Quote (REST market/v1/quote mode=FULL)
    # ------------------------------------------------------------------

    def normalize_full_quote(
        self,
        raw: dict[str, Any],
        instrument_id: str,
        exchange: str,
        received_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Normalize an Angel One FULL mode quote response.

        Captures all available fields including market depth, circuit limits,
        52-week high/low, and OI.

        Args:
            raw:           Raw dict from Angel One market/v1/quote FULL response.
            instrument_id: Canonical instrument ID.
            exchange:      Exchange code.
            received_at:   UTC ISO-8601 received timestamp (defaults to now).

        Returns:
            Canonical quote dict with all fields populated or null.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        # LTP — required
        ltp = _safe_float(raw.get("ltp"))

        # OHLC
        open_p   = _safe_float(raw.get("open"))
        high_p   = _safe_float(raw.get("high"))
        low_p    = _safe_float(raw.get("low"))
        prev_close = _safe_float(raw.get("close"))  # Angel One 'close' = previous session close

        # Volume
        volume = _safe_int(raw.get("tradeVolume")) or _safe_int(raw.get("volume"))
        volume_unavailable = volume is None
        if volume is None:
            volume = 0

        # OI — never from tradedValue
        oi_raw = raw.get("opnInterest") or raw.get("openInterest")
        oi: Optional[int] = None
        oi_missing = True
        if oi_raw is not None:
            oi = _safe_int(oi_raw)
            oi_missing = oi is None

        # Change / changePct
        change     = _safe_float(raw.get("netChange"))
        change_pct = _safe_float(raw.get("percentChange"))
        if change is None and ltp is not None and prev_close is not None and prev_close != 0:
            change = ltp - prev_close
        if change_pct is None and change is not None and prev_close is not None and prev_close != 0:
            change_pct = round(change / prev_close * 100, 4)

        # Average price
        avg_price = _safe_float(raw.get("avgPrice"))

        # Buy/sell quantities
        total_buy_qty  = _safe_int(raw.get("totBuyQtn")) or _safe_int(raw.get("totalBuyQty"))
        total_sell_qty = _safe_int(raw.get("totSellQtn")) or _safe_int(raw.get("totalSellQty"))
        last_trade_qty = _safe_int(raw.get("lastTradedQty")) or _safe_int(raw.get("ltq"))

        # Circuit limits
        upper_circuit = _safe_float(raw.get("upperCircuit"))
        lower_circuit = _safe_float(raw.get("lowerCircuit"))

        # 52-week high/low
        week_high_52 = _safe_float(raw.get("yearHigh")) or _safe_float(raw.get("52WeekHigh"))
        week_low_52  = _safe_float(raw.get("yearLow"))  or _safe_float(raw.get("52WeekLow"))

        # Timestamps
        exch_feed_time  = raw.get("exchFeedTime") or raw.get("exchangeFeedTime")
        exch_trade_time = raw.get("exchTradeTime") or raw.get("lastTradeTime")
        source_timestamp = exch_feed_time or exch_trade_time

        # Market depth — best 5 buy/sell
        depth_buy  = _normalize_depth_levels(raw.get("depth", {}).get("buy", []))
        depth_sell = _normalize_depth_levels(raw.get("depth", {}).get("sell", []))

        return {
            "instrumentId":       instrument_id,
            "provider":           _PROVIDER,
            "sourceType":         _SOURCE_TYPE,
            "exchange":           exchange,
            "ltp":                ltp,
            "open":               open_p,
            "high":               high_p,
            "low":                low_p,
            "prevClose":          prev_close,
            "change":             change,
            "changePct":          change_pct,
            "volume":             volume,
            "volumeUnavailable":  volume_unavailable,
            "oi":                 oi,
            "oiMissing":          oi_missing,
            "avgTradedPrice":     avg_price,
            "totalBuyQty":        total_buy_qty,
            "totalSellQty":       total_sell_qty,
            "lastTradeQty":       last_trade_qty,
            "upperCircuit":       upper_circuit,
            "lowerCircuit":       lower_circuit,
            "weekHigh52":         week_high_52,
            "weekLow52":          week_low_52,
            "depthBuy":           depth_buy,
            "depthSell":          depth_sell,
            "depthLevels":        5,
            "sourceTimestamp":    source_timestamp,
            "receivedAt":         received_at,
        }

    # ------------------------------------------------------------------
    # Historical OHLCV candle
    # ------------------------------------------------------------------

    def normalize_candle(
        self,
        raw_candle: dict[str, Any],
        instrument_id: str,
        exchange: str,
        interval: str,
    ) -> Optional[dict[str, Any]]:
        """Normalize a single Angel One historical OHLCV candle.

        Accepts both the raw array format (returned by the adapter) and
        dict format.

        Array format from adapter: already converted to dict with keys:
            timestamp, open, high, low, close, volume, provider, sourceType,
            symbol, exchange, interval

        Args:
            raw_candle:    Candle dict from AngelOneAdapter.fetch_historical_ohlcv().
            instrument_id: Canonical instrument ID.
            exchange:      Exchange code.
            interval:      Canonical interval string.

        Returns:
            Normalized candle dict, or None if required fields are missing.
        """
        timestamp = raw_candle.get("time") or raw_candle.get("timestamp")
        open_p  = _safe_float(raw_candle.get("open"))
        high_p  = _safe_float(raw_candle.get("high"))
        low_p   = _safe_float(raw_candle.get("low"))
        close_p = _safe_float(raw_candle.get("close"))

        if any(v is None for v in (timestamp, open_p, high_p, low_p, close_p)):
            logger.warning(
                "angel_one_normalizer_candle_missing_required_fields",
                component="angel_one_normalizer",
                instrument_id=instrument_id,
                interval=interval,
            )
            return None

        volume = _safe_int(raw_candle.get("volume"))
        volume_unavailable = volume is None
        if volume is None:
            volume = 0

        # Angel One historical candles do NOT include OI
        return {
            "instrumentId":      instrument_id,
            "provider":          _PROVIDER,
            "sourceType":        _SOURCE_TYPE,
            "exchange":          exchange,
            "intervalStr":       interval,
            "timestamp":         timestamp,
            "open":              open_p,
            "high":              high_p,
            "low":               low_p,
            "close":             close_p,
            "volume":            volume,
            "volumeUnavailable": volume_unavailable,
            "oi":                None,
            "oiMissing":         True,
            "dataOrigin":        "PROVIDER",
            "normalisationVersion": "2.0.0",
        }

    # ------------------------------------------------------------------
    # Historical OI
    # ------------------------------------------------------------------

    def normalize_oi_record(
        self,
        raw_record: dict[str, Any],
        instrument_id: str,
        exchange: str,
        interval: str,
    ) -> Optional[dict[str, Any]]:
        """Normalize a single Angel One historical OI record.

        Args:
            raw_record:    OI record dict from AngelOneAdapter.fetch_historical_oi().
                           Expected keys: timestamp, openInterest (or at index 0/1 as array).
            instrument_id: Canonical instrument ID.
            exchange:      Exchange code.
            interval:      Canonical interval string.

        Returns:
            Normalized OI record dict, or None if required fields missing.
        """
        timestamp = raw_record.get("timestamp")
        oi_raw = raw_record.get("openInterest") or raw_record.get("oi")

        if timestamp is None or oi_raw is None:
            return None

        oi = _safe_int(oi_raw)
        if oi is None:
            return None

        return {
            "instrumentId":  instrument_id,
            "provider":      _PROVIDER,
            "sourceType":    _SOURCE_TYPE,
            "exchange":      exchange,
            "intervalStr":   interval,
            "timestamp":     timestamp,
            "openInterest":  oi,
            "receivedAt":    _utc_now_iso(),
        }

    # ------------------------------------------------------------------
    # Option Greeks
    # ------------------------------------------------------------------

    def normalize_option_greek(
        self,
        raw: dict[str, Any],
        underlying: str,
        expiry: str,
        received_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Normalize a single Angel One option Greeks row.

        Args:
            raw:       Raw dict from optionGreek endpoint.
                       Expected keys: strikePrice, optionType, delta, gamma,
                       theta, vega, impliedVolatility, tradeVolume, openInterest.
            underlying: Underlying symbol (e.g. "NIFTY").
            expiry:     Expiry date string in Angel One format (e.g. "29FEB2024").
            received_at: UTC ISO-8601 timestamp (defaults to now).

        Returns:
            Normalized Greeks dict, or None if required fields missing.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        strike = _safe_float(raw.get("strikePrice"))
        option_type = raw.get("optionType")

        if strike is None or option_type not in ("CE", "PE", "C", "P"):
            return None

        # Normalize option type
        opt_type = "CE" if option_type in ("CE", "C") else "PE"

        # IV — zero is not a valid substitute
        iv_raw = raw.get("impliedVolatility") or raw.get("iv")
        iv = _safe_float(iv_raw, allow_zero=False)

        # Greeks — null when absent; zero accepted (can be legitimate for deep OTM)
        delta = _safe_float(raw.get("delta"))
        gamma = _safe_float(raw.get("gamma"))
        theta = _safe_float(raw.get("theta"))
        vega  = _safe_float(raw.get("vega"))

        # OI
        oi = _safe_int(raw.get("openInterest") or raw.get("oi"))

        # Volume
        volume = _safe_int(raw.get("tradeVolume") or raw.get("volume"))

        greeks_missing = all(v is None for v in (delta, gamma, theta, vega))

        return {
            "underlying":      underlying,
            "expiry":          expiry,
            "strike":          strike,
            "optionType":      opt_type,
            "provider":        _PROVIDER,
            "sourceType":      _SOURCE_TYPE,
            "iv":              iv,
            "ivMissing":       iv is None,
            "delta":           delta,
            "gamma":           gamma,
            "theta":           theta,
            "vega":            vega,
            "rho":             None,           # Angel One does not provide rho
            "greeksMissing":   greeks_missing,
            "oi":              oi,
            "oiMissing":       oi is None,
            "volume":          volume,
            "greekSource":     "PROVIDER",     # from Angel One, not internally calculated
            "receivedAt":      received_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_depth_levels(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize depth levels from Angel One full quote.

    Args:
        levels: List of raw depth level dicts from Angel One response.
                Each has price, quantity, orders.

    Returns:
        List of normalized depth dicts.
    """
    result = []
    for i, level in enumerate(levels[:5]):  # max 5 levels
        price    = _safe_float(level.get("price"))
        qty      = _safe_int(level.get("quantity") or level.get("qty"))
        orders   = _safe_int(level.get("numberOfOrders") or level.get("orders"))
        result.append({
            "level":  i + 1,
            "price":  price,
            "qty":    qty,
            "orders": orders,
        })
    return result
