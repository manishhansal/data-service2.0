"""
src/core/normalizers/upstox.py

Upstox V2/V3 response normalizer for DATA-SERVICE 2.0.

Converts Upstox REST and WebSocket responses into the canonical platform schema.

Handles all Upstox response types:
  - Full Market Quote V2  (from /v2/market-quote/quotes)
  - LTP V3                (from /v3/market-quote/ltp)
  - OHLC V3               (from /v3/market-quote/ohlc)
  - Historical Candle V3  (from /v3/historical-candle/...)
  - Intraday Candle V3    (from /v3/historical-candle/intraday/...)
  - Option Greeks V3      (from /v3/market-quote/option-greek)
  - Option Chain V2       (from /v2/option/chain)
  - WebSocket V3 tick     (Protobuf-decoded normalized dict from upstox_stream.py)
  - Closing Auction       (CAS fields from full quote or WebSocket)

Design contracts
----------------
* OI: never from tradedValue; populated from oi field only.
* IV: zero is NOT a valid substitute — set to None.
* Greeks: None when absent; placeholder zeros prohibited.
* Bid/ask: None when absent; zero prohibited.
* CAS indicative price: stored in cas sub-dict, NEVER as ltp.
* pop (probability of profit) from option chain is a provider-specific
  field — stored as provider_pop, not mixed with canonical Greeks.

Upstox field name mapping → canonical:
  Full quote V2:
    last_price → ltp
    ohlc.{open,high,low,close} → open/high/low/prevClose
    volume → volume
    net_change → change
    lower_circuit_limit / upper_circuit_limit → lowerCircuit / upperCircuit
    oi → oi (F&O only)
    depth.buy[0..4] / depth.sell[0..4] → depthBuy / depthSell
    timestamp → sourceTimestamp

  Option Greeks V3:
    last_price → ltp
    cp → prevClose
    iv → iv
    delta/gamma/theta/vega → Greeks
    oi → oi
    ltq → lastTradeQty
    volume → volume

  Option Chain V2 (per contract):
    market_data.ltp → ltp
    market_data.close_price → prevClose
    market_data.volume → volume
    market_data.oi → oi
    market_data.prev_oi → prevOi
    market_data.bid_price / bid_qty → bid / bidQty
    market_data.ask_price / ask_qty → ask / askQty
    option_greeks.iv → iv
    option_greeks.delta/gamma/theta/vega → Greeks
    option_greeks.pop → provider_pop (NOT canonical)

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

_PROVIDER = "upstox"
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


class UpstoxNormalizer:
    """Normalizes Upstox API responses into canonical platform schema.

    Stateless — safe for concurrent use across async tasks.
    """

    # ------------------------------------------------------------------
    # Full Market Quote V2
    # ------------------------------------------------------------------

    def normalize_full_quote(
        self,
        instrument_key: str,
        raw: dict[str, Any],
        instrument_id: str,
        exchange: str,
        received_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Normalize an Upstox Full Market Quote V2 response.

        Captures OHLC, depth (5 levels), volume, OI, circuit limits,
        net change, and source timestamp.

        Args:
            instrument_key: Upstox instrument key (e.g. "NSE_EQ|INE002A01018").
            raw:            Raw dict for this instrument from /v2/market-quote/quotes.
            instrument_id:  Canonical instrument ID.
            exchange:       Exchange code.
            received_at:    UTC ISO-8601 received timestamp (defaults to now).

        Returns:
            Canonical quote dict with all available fields.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        ltp = _safe_float(raw.get("last_price"))

        # OHLC — nested in raw['ohlc']
        ohlc = raw.get("ohlc", {}) or {}
        open_p    = _safe_float(ohlc.get("open"))
        high_p    = _safe_float(ohlc.get("high"))
        low_p     = _safe_float(ohlc.get("low"))
        prev_close = _safe_float(ohlc.get("close"))

        # Volume
        volume = _safe_int(raw.get("volume"))
        volume_unavailable = volume is None
        if volume is None:
            volume = 0

        # OI — present for F&O, absent for cash
        oi_raw = raw.get("oi")
        oi: Optional[int] = None
        oi_missing = True
        if oi_raw is not None:
            oi = _safe_int(oi_raw)
            oi_missing = oi is None

        # Change
        change = _safe_float(raw.get("net_change"))
        if change is None and ltp is not None and prev_close is not None and prev_close != 0:
            change = ltp - prev_close
        change_pct: Optional[float] = None
        if change is not None and prev_close is not None and prev_close != 0:
            change_pct = round(change / prev_close * 100, 4)

        # Circuit limits
        upper_circuit = _safe_float(raw.get("upper_circuit_limit"))
        lower_circuit = _safe_float(raw.get("lower_circuit_limit"))

        # Source timestamp
        source_timestamp = raw.get("timestamp")

        # Market depth — best 5 buy/sell
        depth_raw = raw.get("depth", {}) or {}
        depth_buy  = _normalize_upstox_depth(depth_raw.get("buy", []))
        depth_sell = _normalize_upstox_depth(depth_raw.get("sell", []))

        # Last trade qty (present in some quote types)
        ltq = _safe_int(raw.get("last_quantity") or raw.get("ltq"))

        return {
            "instrumentId":       instrument_id,
            "instrumentKey":      instrument_key,
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
            "upperCircuit":       upper_circuit,
            "lowerCircuit":       lower_circuit,
            "depthBuy":           depth_buy,
            "depthSell":          depth_sell,
            "depthLevels":        5,
            "lastTradeQty":       ltq,
            "sourceTimestamp":    source_timestamp,
            "receivedAt":         received_at,
        }

    # ------------------------------------------------------------------
    # LTP V3
    # ------------------------------------------------------------------

    def normalize_ltp(
        self,
        instrument_key: str,
        raw: dict[str, Any],
        instrument_id: str,
        exchange: str,
        received_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Normalize an Upstox LTP V3 response.

        LTP V3 adds ltq, volume, and prevClose (cp) vs V2.

        Args:
            instrument_key: Upstox instrument key.
            raw:            Raw LTP response dict for this instrument.
            instrument_id:  Canonical instrument ID.
            exchange:       Exchange code.
            received_at:    UTC ISO-8601 received timestamp.

        Returns:
            Canonical LTP dict.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        ltp = _safe_float(raw.get("last_price"))
        ltq = _safe_int(raw.get("ltq"))
        volume = _safe_int(raw.get("volume"))
        prev_close = _safe_float(raw.get("cp"))

        return {
            "instrumentId":  instrument_id,
            "instrumentKey": instrument_key,
            "provider":      _PROVIDER,
            "sourceType":    _SOURCE_TYPE,
            "exchange":      exchange,
            "ltp":           ltp,
            "lastTradeQty":  ltq,
            "volume":        volume,
            "prevClose":     prev_close,
            "receivedAt":    received_at,
        }

    # ------------------------------------------------------------------
    # Historical Candle V3 (already normalized by adapter; re-validate)
    # ------------------------------------------------------------------

    def normalize_candle(
        self,
        raw_candle: dict[str, Any],
        instrument_id: str,
        exchange: str,
        interval: str,
    ) -> Optional[dict[str, Any]]:
        """Normalize a single Upstox V3 historical candle (from adapter output).

        The adapter already extracts named fields from the raw array.
        This function re-validates and attaches canonical metadata.

        Args:
            raw_candle:    Dict with keys: timestamp, open, high, low, close,
                           volume, open_interest (from UpstoxAdapter).
            instrument_id: Canonical instrument ID.
            exchange:      Exchange code.
            interval:      Canonical interval string.

        Returns:
            Normalized candle dict or None if required fields missing.
        """
        timestamp = raw_candle.get("timestamp")
        open_p  = _safe_float(raw_candle.get("open"))
        high_p  = _safe_float(raw_candle.get("high"))
        low_p   = _safe_float(raw_candle.get("low"))
        close_p = _safe_float(raw_candle.get("close"))

        if any(v is None for v in (timestamp, open_p, high_p, low_p, close_p)):
            logger.warning(
                "upstox_normalizer_candle_missing_fields",
                component="upstox_normalizer",
                instrument_id=instrument_id,
                interval=interval,
            )
            return None

        volume = _safe_int(raw_candle.get("volume"))
        volume_unavailable = volume is None
        if volume is None:
            volume = 0

        # V3 includes OI at index 6 — never treat 0 as missing for derivatives
        oi_raw = raw_candle.get("open_interest")
        oi: Optional[int] = None
        oi_missing = True
        if oi_raw is not None:
            oi = _safe_int(oi_raw)
            oi_missing = oi is None

        return {
            "instrumentId":         instrument_id,
            "provider":             _PROVIDER,
            "sourceType":           _SOURCE_TYPE,
            "exchange":             exchange,
            "intervalStr":          interval,
            "timestamp":            timestamp,
            "open":                 open_p,
            "high":                 high_p,
            "low":                  low_p,
            "close":                close_p,
            "volume":               volume,
            "volumeUnavailable":    volume_unavailable,
            "oi":                   oi,
            "oiMissing":            oi_missing,
            "dataOrigin":           "PROVIDER",
            "apiVersion":           raw_candle.get("api_version", "v3"),
            "normalisationVersion": "2.0.0",
        }

    # ------------------------------------------------------------------
    # Option Greeks V3
    # ------------------------------------------------------------------

    def normalize_option_greek(
        self,
        instrument_key: str,
        raw: dict[str, Any],
        instrument_id: str,
        received_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Normalize an Upstox Option Greeks V3 response for one instrument.

        Args:
            instrument_key: Upstox instrument key.
            raw:            Raw Greeks dict for this instrument from
                            /v3/market-quote/option-greek.
            instrument_id:  Canonical instrument ID.
            received_at:    UTC ISO-8601 received timestamp.

        Returns:
            Normalized Greeks dict or None if required fields missing.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        ltp = _safe_float(raw.get("last_price"))
        prev_close = _safe_float(raw.get("cp"))
        ltq = _safe_int(raw.get("ltq"))
        volume = _safe_int(raw.get("volume"))

        # OI
        oi_raw = raw.get("oi")
        oi: Optional[int] = None
        oi_missing = True
        if oi_raw is not None:
            oi = _safe_int(oi_raw)
            oi_missing = oi is None

        # IV — zero is not a substitute
        iv_raw = raw.get("iv")
        iv = _safe_float(iv_raw, allow_zero=False)

        # Greeks — zero is accepted (e.g. deep OTM delta ≈ 0)
        delta = _safe_float(raw.get("delta"))
        gamma = _safe_float(raw.get("gamma"))
        theta = _safe_float(raw.get("theta"))
        vega  = _safe_float(raw.get("vega"))
        # Note: Upstox V3 option-greek does not include rho
        rho = None

        greeks_missing = all(v is None for v in (delta, gamma, theta, vega))

        return {
            "instrumentId":   instrument_id,
            "instrumentKey":  instrument_key,
            "provider":       _PROVIDER,
            "sourceType":     _SOURCE_TYPE,
            "ltp":            ltp,
            "prevClose":      prev_close,
            "lastTradeQty":   ltq,
            "volume":         volume,
            "oi":             oi,
            "oiMissing":      oi_missing,
            "iv":             iv,
            "ivMissing":      iv is None,
            "delta":          delta,
            "gamma":          gamma,
            "theta":          theta,
            "vega":           vega,
            "rho":            rho,
            "greeksMissing":  greeks_missing,
            "greekSource":    "PROVIDER",
            "receivedAt":     received_at,
        }

    # ------------------------------------------------------------------
    # Option Chain V2
    # ------------------------------------------------------------------

    def normalize_option_chain_contract(
        self,
        contract: dict[str, Any],
        underlying: str,
        expiry: str,
        spot_price: Optional[float],
        received_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Normalize a single Upstox option chain contract row.

        Handles both CE and PE contracts from /v2/option/chain response.
        The pop (probability of profit) field is preserved as provider_pop
        since it is Upstox-specific and not a canonical Greek.

        Args:
            contract:   Raw contract dict from Upstox option chain response.
                        Typically has structure:
                        {strike_price, expiry, instrument_key,
                         call_options: {market_data, option_greeks},
                         put_options: {market_data, option_greeks}}
            underlying: Underlying symbol.
            expiry:     Expiry date as "YYYY-MM-DD".
            spot_price: Current spot price (for ATM calculation).
            received_at: UTC ISO-8601 timestamp.

        Returns:
            List of normalized contract dicts (one for CE, one for PE),
            or None if required fields missing.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        strike = _safe_float(contract.get("strike_price"))
        if strike is None:
            return None

        normalized_contracts = []

        for opt_type, key in (("CE", "call_options"), ("PE", "put_options")):
            option_data = contract.get(key, {}) or {}
            if not option_data:
                continue

            market_data = option_data.get("market_data", {}) or {}
            option_greeks = option_data.get("option_greeks", {}) or {}

            instrument_key = option_data.get("instrument_key", "")
            instrument_id = instrument_key  # will be resolved to canonical ID by caller

            ltp       = _safe_float(market_data.get("ltp"))
            prev_close = _safe_float(market_data.get("close_price"))
            volume    = _safe_int(market_data.get("volume"))
            oi_raw    = market_data.get("oi")
            prev_oi   = _safe_int(market_data.get("prev_oi"))
            bid       = _safe_float(market_data.get("bid_price"), allow_zero=False)
            bid_qty   = _safe_int(market_data.get("bid_qty"))
            ask       = _safe_float(market_data.get("ask_price"), allow_zero=False)
            ask_qty   = _safe_int(market_data.get("ask_qty"))

            oi: Optional[int] = None
            oi_missing = True
            if oi_raw is not None:
                oi = _safe_int(oi_raw)
                oi_missing = oi is None

            # IV — zero is NOT a substitute
            iv  = _safe_float(option_greeks.get("iv"), allow_zero=False)
            pop = _safe_float(option_greeks.get("pop"))  # provider-specific, not canonical Greek

            # Greeks
            delta = _safe_float(option_greeks.get("delta"))
            gamma = _safe_float(option_greeks.get("gamma"))
            theta = _safe_float(option_greeks.get("theta"))
            vega  = _safe_float(option_greeks.get("vega"))
            greeks_missing = all(v is None for v in (delta, gamma, theta, vega))

            # ATM flag
            is_atm = False
            if spot_price is not None and strike is not None:
                is_atm = abs(strike - spot_price) == min(
                    abs(s - spot_price)
                    for s in [strike]  # will be replaced by caller with all strikes
                )

            normalized_contracts.append({
                "underlying":       underlying,
                "expiry":           expiry,
                "strike":           strike,
                "optionType":       opt_type,
                "instrumentKey":    instrument_key,
                "instrumentId":     instrument_id,
                "provider":         _PROVIDER,
                "sourceType":       _SOURCE_TYPE,
                "ltp":              ltp,
                "prevClose":        prev_close,
                "volume":           volume,
                "oi":               oi,
                "oiMissing":        oi_missing,
                "prevOi":           prev_oi,
                "bid":              bid,
                "bidQty":           bid_qty,
                "ask":              ask,
                "askQty":           ask_qty,
                "bidAskMissing":    bid is None and ask is None,
                "iv":               iv,
                "ivMissing":        iv is None,
                "delta":            delta,
                "gamma":            gamma,
                "theta":            theta,
                "vega":             vega,
                "rho":              None,  # Upstox option chain does not include rho
                "greeksMissing":    greeks_missing,
                "providerPop":      pop,   # Upstox-specific; NOT a canonical Greek
                "greekSource":      "PROVIDER",
                "isAtm":            is_atm,
                "receivedAt":       received_at,
            })

        return normalized_contracts if normalized_contracts else None

    def normalize_option_chain(
        self,
        raw_chain: dict[str, Any],
        underlying: str,
        expiry: str,
        received_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Normalize an entire Upstox option chain response.

        Args:
            raw_chain:  Raw response from UpstoxAdapter.fetch_option_chain().
            underlying: Underlying symbol.
            expiry:     Expiry date as "YYYY-MM-DD".
            received_at: UTC ISO-8601 timestamp.

        Returns:
            Canonical option chain dict with normalized contracts and
            chain-level analytics.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        pcr = _safe_float(raw_chain.get("pcr"))
        spot_price = _safe_float(
            (raw_chain.get("underlying_spot_price", {}) or {}).get("last_price")
        )

        # Contracts may be a list directly or nested under a key
        raw_contracts = raw_chain.get("contracts") or raw_chain.get("data") or []
        if isinstance(raw_contracts, dict):
            raw_contracts = list(raw_contracts.values())

        # Get all unique strikes for ATM calculation
        all_strikes = []
        for c in raw_contracts:
            s = _safe_float(c.get("strike_price"))
            if s is not None:
                all_strikes.append(s)

        normalized_all: list[dict[str, Any]] = []
        for contract in raw_contracts:
            result = self.normalize_option_chain_contract(
                contract, underlying, expiry, spot_price, received_at
            )
            if result:
                # Fix ATM flag now that we have all strikes
                if spot_price is not None and all_strikes:
                    atm_strike = min(all_strikes, key=lambda s: abs(s - spot_price))
                    for c in result:
                        c["isAtm"] = c["strike"] == atm_strike
                normalized_all.extend(result)

        return {
            "underlying":    underlying,
            "expiry":        expiry,
            "spotPrice":     spot_price,
            "pcr":           pcr,
            "provider":      _PROVIDER,
            "sourceType":    _SOURCE_TYPE,
            "contracts":     normalized_all,
            "contractCount": len(normalized_all),
            "receivedAt":    received_at,
        }

    # ------------------------------------------------------------------
    # Closing Auction Session (CAS) data
    # ------------------------------------------------------------------

    def normalize_cas_data(
        self,
        instrument_key: str,
        cas_raw: dict[str, Any],
        instrument_id: str,
        exchange: str,
        session_date: str,
        received_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Normalize Upstox CAS (Closing Auction Session) data.

        CRITICAL: The indicative_equilibrium_price is NOT a traded LTP.
        It must be stored in ClosingAuctionSnapshot, not in MarketTick or
        MarketQuote.

        Args:
            instrument_key: Upstox instrument key.
            cas_raw:        CAS sub-dict from full quote or WebSocket tick.
            instrument_id:  Canonical instrument ID.
            exchange:       Exchange code.
            session_date:   Session date as "YYYY-MM-DD".
            received_at:    UTC ISO-8601 timestamp.

        Returns:
            Normalized CAS dict suitable for ClosingAuctionSnapshot, or None.
        """
        if received_at is None:
            received_at = _utc_now_iso()

        ieq_price = _safe_float(cas_raw.get("indicative_equilibrium_price"))
        ieq_qty   = _safe_int(cas_raw.get("indicative_equilibrium_quantity"))
        total_qty = _safe_int(cas_raw.get("total_indicative_quantity"))
        imbalance = _safe_int(cas_raw.get("market_indicative_imbalance"))
        ref_price = _safe_float(cas_raw.get("reference_price"))

        if all(v is None for v in (ieq_price, ieq_qty, total_qty, imbalance, ref_price)):
            return None

        return {
            "instrumentId":                  instrument_id,
            "instrumentKey":                 instrument_key,
            "exchange":                      exchange,
            "sessionDate":                   session_date,
            "provider":                      _PROVIDER,
            "sourceType":                    _SOURCE_TYPE,
            # CAS indicative price — EXPLICITLY NOT LTP
            "indicativeEquilibriumPrice":    ieq_price,
            "indicativeEquilibriumQuantity": ieq_qty,
            "totalIndicativeQuantity":       total_qty,
            "marketIndicativeImbalance":     imbalance,
            "referencePrice":                ref_price,
            "receivedAt":                    received_at,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_upstox_depth(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Upstox market depth levels.

    Args:
        levels: List of raw depth dicts from Upstox /v2/market-quote/quotes.
                Each has quantity, price, orders.

    Returns:
        Normalized depth list with level numbers.
    """
    result = []
    for i, level in enumerate(levels[:5]):
        price  = _safe_float(level.get("price"))
        qty    = _safe_int(level.get("quantity"))
        orders = _safe_int(level.get("orders"))
        result.append({
            "level":  i + 1,
            "price":  price,
            "qty":    qty,
            "orders": orders,
        })
    return result
