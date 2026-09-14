"""
src/engines/market_engine.py

NSE Market Engine — live quote acquisition, normalisation, and option chain
pipeline.

Responsibilities (Tasks 6.3, 6.4):
  - ``get_live_quote(instrument_id, exchange)`` — returns a fully normalised
    live quote dict.  During CLOSED / PRE_OPEN / POST_MARKET sessions the
    last cached quote is returned with ``marketStatus`` reflecting the actual
    phase.  When the session is CLOSED the call is NEVER classified as a
    provider failure and circuit-breaker counters are NOT incremented.
  - ``get_option_chain(underlying, expiry)`` — returns a normalised option
    chain snapshot with per-row completeness flags, option analytics
    (pcrOi, pcrVolume, maxCeOiStrike, maxPeOiStrike, totalCeOi, totalPeOi,
    atmIv, maxPain), and MetricTag annotations on every computed field.
  - ``get_current_session_phase()`` — convenience wrapper around the
    MarketSessionEngine.

Phase-specific behaviours:
  - CLOSED / PRE_OPEN / POST_MARKET: return last cached data (no older than
    24 hours) with the appropriate ``marketStatus``.  The engine does NOT
    increment any circuit-breaker failure counter for these conditions
    (Requirements 3.4, 12.5).
  - REGULAR: acquire live data from the provider stub.  In this version the
    provider stub returns simulated data until Phase 8 (Streaming Engine)
    wires real broker WebSocket feeds.

Null semantics (non-negotiable, Requirements 3.3, 6.2–6.6):
  - ``oi``: NEVER from tradedValue; null + oiMissing=True when absent.
  - ``iv``: null when absent; zero is NOT a substitute.
  - Greeks (delta, gamma, theta, vega, rho): null when absent; zeros
    prohibited as placeholders.
  - ``bid`` / ``ask``: null when absent; zero prohibited.

Option chain analytics MetricTag assignments:
  - OBSERVED: strike, optionType, ltp, oi, oiChange, volume, tradedValue,
    bid, ask
  - MODELLED: iv, delta, gamma, theta, vega, rho
  - DERIVED: pcrOi, pcrVolume, maxCeOiStrike, maxPeOiStrike, totalCeOi,
    totalPeOi, atmIv, maxPain

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 12.5
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.normaliser import MetricTag, Normaliser
from src.core.schemas.instrument import SessionPhase
from src.engines.holiday_calendar import HolidayCalendar
from src.engines.market_session import MarketSessionEngine
from src.observability.logging import get_logger

# AngelOneAdapter — optional; engine still boots without credentials.
# When configured, live quote and option chain are served from real Angel One
# SmartAPI rather than the Phase-8 stub.
try:
    from src.providers.adapters.angel_one import AngelOneAdapter as _AngelOneAdapter
    _ANGEL_ONE_AVAILABLE = True
except ImportError:
    _AngelOneAdapter = None  # type: ignore[assignment,misc]
    _ANGEL_ONE_AVAILABLE = False

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum age (seconds) of a cached quote that may be served during a
#: non-REGULAR session (Requirement 12.5).
_MAX_CACHED_QUOTE_AGE_SEC: int = 24 * 60 * 60  # 24 hours

#: Maximum age (seconds) of the underlying spot price before an option
#: chain is marked DEGRADED (Requirement 3.8).
_SPOT_AGE_DEGRADED_SEC: int = 60

#: Normalisation version attached to every provenance record.
_NORMALISATION_VERSION: str = "2.0.0"


# ---------------------------------------------------------------------------
# MarketEngine
# ---------------------------------------------------------------------------


class MarketEngine:
    """Acquires, normalises, and serves live Indian market data.

    The engine uses ``MarketSessionEngine`` for session classification and
    ``HolidayCalendar`` for holiday lookups.  Provider calls in this
    implementation are stubs that return realistic empty/minimal structures;
    Phase 8 (Streaming Engine) replaces these stubs with real broker WebSocket
    feeds.

    Thread / asyncio safety: all public methods are async-safe.  The
    ``_quote_cache`` dict is mutated only by ``_update_quote_cache``, which
    is called from the event loop thread.
    """

    def __init__(
        self,
        session_engine: Optional[MarketSessionEngine] = None,
        holiday_calendar: Optional[HolidayCalendar] = None,
        normaliser: Optional[Normaliser] = None,
        angel_one_adapter: Optional[Any] = None,
    ) -> None:
        """Initialise the Market Engine.

        Args:
            session_engine: ``MarketSessionEngine`` instance for IST session
                classification.  When ``None`` a default instance is created
                using the provided ``holiday_calendar``.
            holiday_calendar: ``HolidayCalendar`` instance.  When ``None``,
                the global singleton is used if available, otherwise session
                classification falls back to weekend-only checks.
            normaliser: ``Normaliser`` instance.  When ``None`` a default
                instance is created.
            angel_one_adapter: Optional ``AngelOneAdapter`` instance.  When
                provided, live quotes and option chains are served from real
                Angel One SmartAPI instead of the Phase-8 stub.  When
                ``None`` the engine degrades to returning stub (null) values.
        """
        # Resolve holiday calendar
        if holiday_calendar is None:
            try:
                holiday_calendar = HolidayCalendar.get_instance()
            except Exception:  # noqa: BLE001
                holiday_calendar = None

        self._calendar: Optional[HolidayCalendar] = holiday_calendar

        # Resolve session engine
        if session_engine is None:
            session_engine = MarketSessionEngine(
                holiday_calendar=holiday_calendar
            )
        self._session_engine: MarketSessionEngine = session_engine

        # Normaliser
        self._normaliser: Normaliser = normaliser or Normaliser()

        # Angel One adapter for live data — None means stub mode
        self._angel_one: Optional[Any] = angel_one_adapter

        # In-memory last-known-quote store keyed by "<exchange>:<instrumentId>"
        self._quote_cache: dict[str, dict] = {}

        # In-memory last-known spot-price store for option chain staleness check
        # keyed by "<exchange>:<underlying>", value: {"price": float, "ts_sec": float}
        self._spot_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public: session
    # ------------------------------------------------------------------

    def get_current_session_phase(
        self, dt_utc: Optional[datetime] = None
    ) -> SessionPhase:
        """Return the current (or specified) NSE session phase.

        A convenience wrapper around ``MarketSessionEngine.get_current_phase``.

        Args:
            dt_utc: UTC datetime.  Defaults to now.

        Returns:
            Current ``SessionPhase``.
        """
        return self._session_engine.get_current_phase(dt_utc)

    # ------------------------------------------------------------------
    # Public: live quote
    # ------------------------------------------------------------------

    async def get_live_quote(
        self,
        instrument_id: str,
        exchange: str = "NSE",
    ) -> dict:
        """Return a fully normalised live quote for *instrument_id*.

        Phase-specific behaviour:
        - **REGULAR**: acquire from provider stub, normalise, update cache,
          return with ``marketStatus: "REGULAR"``.
        - **CLOSED / PRE_OPEN / PRE_OPEN_CALL_AUCTION / POST_MARKET /
          MUHURAT**: return the last cached quote (no older than 24 hours)
          with ``marketStatus`` set to the actual phase name.  When no
          cached quote is available return a minimal response with
          ``marketStatus`` and ``ltp: null``.  The call is NEVER classified
          as a provider failure and circuit-breaker counters are NOT
          incremented (Requirements 3.4, 12.5).

        Args:
            instrument_id: Canonical instrument identifier (e.g.
                ``"NSE:NIFTY:IDX"``).
            exchange: Exchange identifier (default ``"NSE"``).

        Returns:
            Normalised quote dict containing all required fields.
        """
        phase = self.get_current_session_phase()
        cache_key = f"{exchange}:{instrument_id}"
        now_sec = time.monotonic()

        if phase == SessionPhase.REGULAR:
            # ── Live acquisition ──────────────────────────────────────────
            raw = await self._fetch_live_quote_stub(instrument_id, exchange)
            provider_name = raw.get("provider", "angel_one" if self._angel_one else "stub")
            normalised, ok, incident = self._normaliser.normalise_quote(raw, provider=provider_name)

            if not ok:
                logger.warning(
                    "market_engine_normalisation_failed",
                    component="market_engine",
                    instrument_id=instrument_id,
                    exchange=exchange,
                    incident=incident,
                )
                # Return last cached quote on normalisation failure
                return self._build_cached_or_empty(
                    cache_key, instrument_id, exchange, phase, now_sec
                )

            # Attach session + provenance
            normalised["marketStatus"] = phase.value
            normalised["provenance"] = _build_provenance(instrument_id, raw.get("provider", "angel_one" if self._angel_one else "stub"))

            # Update cache
            self._quote_cache[cache_key] = {
                "quote": normalised,
                "ts_sec": now_sec,
            }

            return normalised

        else:
            # ── Non-REGULAR: return cached data ──────────────────────────
            return self._build_cached_or_empty(
                cache_key, instrument_id, exchange, phase, now_sec
            )

    def _build_cached_or_empty(
        self,
        cache_key: str,
        instrument_id: str,
        exchange: str,
        phase: SessionPhase,
        now_sec: float,
    ) -> dict:
        """Return the last cached quote or a minimal empty-quote structure.

        If a cached quote exists and is no older than 24 hours, return it
        with the ``marketStatus`` updated to the current phase.

        Args:
            cache_key: Cache lookup key.
            instrument_id: Instrument identifier.
            exchange: Exchange identifier.
            phase: Current session phase.
            now_sec: Current monotonic time in seconds.

        Returns:
            Quote dict (from cache or empty sentinel).
        """
        entry = self._quote_cache.get(cache_key)
        if entry is not None:
            age_sec = now_sec - entry["ts_sec"]
            if age_sec <= _MAX_CACHED_QUOTE_AGE_SEC:
                # Return a shallow copy with the status updated
                quote = dict(entry["quote"])
                quote["marketStatus"] = phase.value
                return quote

        # No valid cache — return minimal sentinel with null fields
        return _empty_quote(instrument_id, exchange, phase.value)

    # ------------------------------------------------------------------
    # Public: option chain
    # ------------------------------------------------------------------

    async def get_option_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
        exchange: str = "NSE",
    ) -> dict:
        """Return a normalised option chain snapshot for *underlying*.

        Behaviour:
        - When market is CLOSED: ``rows: []``, ``marketStatus: "CLOSED"``
          (NOT HTTP 5xx — caller handles HTTP).
        - When no contracts are listed: ``rows: []``,
          ``marketStatus: "NO_DATA"``.
        - When spot price is older than 60 seconds: ``chainQuality:
          "DEGRADED"``, ``spotAgeMs`` included.
        - Analytics are computed over the full row set and each computed
          field is tagged with its ``MetricTag``.

        Args:
            underlying: Underlying symbol (e.g. ``"NIFTY"``).
            expiry: Expiry filter as ISO-8601 date string (``YYYY-MM-DD``).
                When ``None``, the nearest expiry is used.
            exchange: Exchange identifier (default ``"NSE"``).

        Returns:
            Option chain snapshot dict with ``rows``, ``analytics``,
            ``chainQuality``, ``marketStatus``, ``underlying``, ``expiry``,
            and ``provenance``.
        """
        phase = self.get_current_session_phase()

        # ── CLOSED session: return empty rows immediately ─────────────────
        if phase == SessionPhase.CLOSED:
            return {
                "underlying": underlying,
                "expiry": expiry,
                "exchange": exchange,
                "rows": [],
                "analytics": _empty_analytics(),
                "chainQuality": "UNKNOWN",
                "marketStatus": SessionPhase.CLOSED.value,
                "provenance": _build_provenance(underlying, "stub"),
            }

        # ── Fetch raw option chain from provider stub ─────────────────────
        raw_rows, raw_spot, raw_expiry = await self._fetch_option_chain_stub(
            underlying, expiry, exchange
        )

        if not raw_rows:
            return {
                "underlying": underlying,
                "expiry": expiry or raw_expiry,
                "exchange": exchange,
                "rows": [],
                "analytics": _empty_analytics(),
                "chainQuality": "UNKNOWN",
                "marketStatus": "NO_DATA",
                "provenance": _build_provenance(underlying, "stub"),
            }

        # ── Normalise each row ────────────────────────────────────────────
        normalised_rows: list[dict] = []
        for raw_row in raw_rows:
            norm, ok, _ = self._normaliser.normalise_option_chain_row(
                raw_row, provider="stub"
            )
            if ok:
                normalised_rows.append(norm)

        if not normalised_rows:
            return {
                "underlying": underlying,
                "expiry": expiry or raw_expiry,
                "exchange": exchange,
                "rows": [],
                "analytics": _empty_analytics(),
                "chainQuality": "UNKNOWN",
                "marketStatus": "NO_DATA",
                "provenance": _build_provenance(underlying, "stub"),
            }

        # ── Check spot price staleness ────────────────────────────────────
        now_sec = time.monotonic()
        spot_key = f"{exchange}:{underlying}"
        chain_quality = "OK"
        spot_age_ms: Optional[int] = None

        if raw_spot is not None:
            spot_entry = self._spot_cache.get(spot_key)
            if spot_entry is None:
                # First time — store it
                self._spot_cache[spot_key] = {
                    "price": raw_spot,
                    "ts_sec": now_sec,
                }
            else:
                spot_age_sec = now_sec - spot_entry["ts_sec"]
                spot_age_ms = int(spot_age_sec * 1000)
                if spot_age_sec > _SPOT_AGE_DEGRADED_SEC:
                    chain_quality = "DEGRADED"
                # Update cache with fresh price
                self._spot_cache[spot_key] = {
                    "price": raw_spot,
                    "ts_sec": now_sec,
                }

        # ── Compute analytics ─────────────────────────────────────────────
        analytics = _compute_analytics(normalised_rows, raw_spot)

        # ── Build response ────────────────────────────────────────────────
        result: dict[str, Any] = {
            "underlying": underlying,
            "expiry": expiry or raw_expiry,
            "exchange": exchange,
            "rows": normalised_rows,
            "analytics": analytics,
            "chainQuality": chain_quality,
            "marketStatus": phase.value,
            "provenance": _build_provenance(underlying, "stub"),
        }

        if spot_age_ms is not None:
            result["spotAgeMs"] = spot_age_ms

        return result

    # ------------------------------------------------------------------
    # Provider stubs / real provider dispatch (Phase 8 wiring)
    # ------------------------------------------------------------------

    async def _fetch_live_quote_stub(
        self, instrument_id: str, exchange: str
    ) -> dict:
        """Fetch live quote — delegates to AngelOneAdapter when configured.

        When ``self._angel_one`` is set, the real Angel One SmartAPI quote
        endpoint is called and the response is returned as-is for the
        normaliser.  When not configured, returns a null-filled dict so the
        normaliser produces a ``ltp: null`` response (market-closed or
        unconfigured — never fabricated data).

        Args:
            instrument_id: Instrument identifier (symbol or token).
            exchange: Exchange identifier.

        Returns:
            Raw quote dict ready for ``Normaliser.normalise_quote()``.
        """
        if self._angel_one is not None:
            try:
                # Attempt a real Angel One SmartAPI quote fetch.
                # fetch_live_quote returns a list of quotes; take first.
                raw_quotes = await self._angel_one.fetch_live_quote(
                    [instrument_id], exchange=exchange
                )
                if raw_quotes:
                    q = raw_quotes[0]
                    # Normalise keys to what the Normaliser expects
                    return {
                        "instrumentId": instrument_id,
                        "symbol": q.get("tradingSymbol", instrument_id),
                        "exchange": exchange,
                        "ltp": q.get("ltp"),
                        "open": q.get("open"),
                        "high": q.get("high"),
                        "low": q.get("low"),
                        "prevClose": q.get("close"),
                        "change": q.get("netChange"),
                        "changePct": q.get("percentChange"),
                        "volume": q.get("tradeVolume"),
                        "oi": q.get("openInterest"),
                        "tradedValue": None,
                        "totalBuyQty": q.get("totBuyQuan"),
                        "totalSellQty": q.get("totSellQuan"),
                        "upperCircuit": q.get("upperCircuit"),
                        "lowerCircuit": q.get("lowerCircuit"),
                        "weekHigh52": q.get("52WeekHigh"),
                        "weekLow52": q.get("52WeekLow"),
                        "lastTradeTime": None,
                        "bid": None,
                        "ask": None,
                        "marketStatus": "REGULAR",
                        "provider": "angel_one",
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "market_engine_angel_quote_failed",
                    component="market_engine",
                    instrument_id=instrument_id,
                    error=str(exc),
                )
                # Fall through to null-filled stub on provider failure
                # so a provider failure is never silently converted to
                # fabricated data (Absolute Rule 2).

        # Stub / fallback — null values, provider="unavailable"
        return {
            "instrumentId": instrument_id,
            "symbol": instrument_id.split(":")[-1] if ":" in instrument_id else instrument_id,
            "exchange": exchange,
            "ltp": None,
            "open": None,
            "high": None,
            "low": None,
            "prevClose": None,
            "change": None,
            "changePct": None,
            "volume": None,
            "oi": None,
            "tradedValue": None,
            "totalBuyQty": None,
            "totalSellQty": None,
            "upperCircuit": None,
            "lowerCircuit": None,
            "weekHigh52": None,
            "weekLow52": None,
            "lastTradeTime": None,
            "bid": None,
            "ask": None,
            "marketStatus": "REGULAR",
            "provider": "unavailable",
        }

    async def _fetch_option_chain_stub(
        self, underlying: str, expiry: Optional[str], exchange: str
    ) -> tuple[list[dict], Optional[float], Optional[str]]:
        """Fetch option chain — delegates to AngelOneAdapter when configured.

        When ``self._angel_one`` is set, the real Angel One option chain is
        fetched.  Otherwise returns empty rows (not fabricated data).

        Args:
            underlying: Underlying symbol.
            expiry: Expiry filter (ISO-8601) or None for nearest expiry.
            exchange: Exchange.

        Returns:
            Tuple of (raw_rows, spot_price, resolved_expiry).
        """
        if self._angel_one is not None:
            try:
                chain_data = await self._angel_one.fetch_option_chain(
                    underlying, expiry=expiry
                )
                if chain_data:
                    raw_rows = chain_data.get("rows", [])
                    spot = chain_data.get("spot")
                    resolved_expiry = chain_data.get("expiry", expiry)
                    return raw_rows, spot, resolved_expiry
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "market_engine_angel_option_chain_failed",
                    component="market_engine",
                    underlying=underlying,
                    error=str(exc),
                )

        # No adapter or adapter failed — empty rows, not fabricated data
        return [], None, expiry

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _update_quote_cache(self, cache_key: str, quote: dict) -> None:
        """Update the in-memory quote cache.

        Args:
            cache_key: Cache key.
            quote: Normalised quote dict.
        """
        self._quote_cache[cache_key] = {
            "quote": quote,
            "ts_sec": time.monotonic(),
        }

    def clear_quote_cache(self) -> None:
        """Clear the in-memory quote cache (used in tests)."""
        self._quote_cache.clear()

    def clear_spot_cache(self) -> None:
        """Clear the in-memory spot price cache (used in tests)."""
        self._spot_cache.clear()


# ---------------------------------------------------------------------------
# Analytics computation
# ---------------------------------------------------------------------------


def _compute_analytics(
    rows: list[dict],
    spot_price: Optional[float],
) -> dict:
    """Compute option chain analytics from normalised rows.

    Computed analytics and their MetricTag:
    - ``pcrOi``         (DERIVED): put/call OI ratio
    - ``pcrVolume``     (DERIVED): put/call volume ratio
    - ``maxCeOiStrike`` (DERIVED): strike with max CE open interest
    - ``maxPeOiStrike`` (DERIVED): strike with max PE open interest
    - ``totalCeOi``     (DERIVED): total call open interest
    - ``totalPeOi``     (DERIVED): total put open interest
    - ``atmIv``         (DERIVED): IV of contract closest to spot
    - ``maxPain``       (DERIVED): max-pain strike

    Args:
        rows: List of normalised option chain rows.
        spot_price: Underlying spot price (or None when unavailable).

    Returns:
        Analytics dict with ``_metricTags`` annotation.
    """
    ce_rows = [r for r in rows if r.get("optionType") in ("CE", "C")]
    pe_rows = [r for r in rows if r.get("optionType") in ("PE", "P")]

    # ── OI totals ─────────────────────────────────────────────────────────
    total_ce_oi: int = sum(r["oi"] for r in ce_rows if r.get("oi") is not None)
    total_pe_oi: int = sum(r["oi"] for r in pe_rows if r.get("oi") is not None)

    # ── PCR OI (null when call OI is zero or null) ────────────────────────
    pcr_oi: Optional[float] = None
    if total_ce_oi > 0:
        pcr_oi = round(total_pe_oi / total_ce_oi, 4)

    # ── Volume totals ─────────────────────────────────────────────────────
    total_ce_vol: int = sum(r["volume"] for r in ce_rows if r.get("volume") is not None)
    total_pe_vol: int = sum(r["volume"] for r in pe_rows if r.get("volume") is not None)

    # ── PCR Volume (null when call volume is zero or null) ────────────────
    pcr_volume: Optional[float] = None
    if total_ce_vol > 0:
        pcr_volume = round(total_pe_vol / total_ce_vol, 4)

    # ── Max OI strikes ─────────────────────────────────────────────────────
    max_ce_oi_strike: Optional[float] = None
    max_pe_oi_strike: Optional[float] = None

    if ce_rows:
        ce_with_oi = [r for r in ce_rows if r.get("oi") is not None]
        if ce_with_oi:
            max_ce_row = max(ce_with_oi, key=lambda r: r["oi"])
            max_ce_oi_strike = max_ce_row.get("strike")

    if pe_rows:
        pe_with_oi = [r for r in pe_rows if r.get("oi") is not None]
        if pe_with_oi:
            max_pe_row = max(pe_with_oi, key=lambda r: r["oi"])
            max_pe_oi_strike = max_pe_row.get("strike")

    # ── ATM IV ────────────────────────────────────────────────────────────
    atm_iv: Optional[float] = None
    if spot_price is not None:
        # Find the row whose strike is closest to spot_price with a valid IV
        rows_with_iv = [r for r in rows if r.get("iv") is not None and r.get("strike") is not None]
        if rows_with_iv:
            atm_row = min(rows_with_iv, key=lambda r: abs(r["strike"] - spot_price))
            atm_iv = atm_row.get("iv")

    # ── Max Pain ──────────────────────────────────────────────────────────
    # Max pain: the strike at which total options OI monetary loss is minimised.
    # For each unique strike, compute the total OI-weighted loss that would occur
    # if the underlying expired at that strike, then pick the strike that
    # minimises aggregate loss across all contracts.
    max_pain: Optional[float] = _compute_max_pain(rows)

    analytics = {
        "pcrOi": pcr_oi,
        "pcrVolume": pcr_volume,
        "maxCeOiStrike": max_ce_oi_strike,
        "maxPeOiStrike": max_pe_oi_strike,
        "totalCeOi": total_ce_oi,
        "totalPeOi": total_pe_oi,
        "atmIv": atm_iv,
        "maxPain": max_pain,
        # MetricTag annotations for all computed fields
        "_metricTags": {
            "pcrOi": MetricTag.DERIVED.value,
            "pcrVolume": MetricTag.DERIVED.value,
            "maxCeOiStrike": MetricTag.DERIVED.value,
            "maxPeOiStrike": MetricTag.DERIVED.value,
            "totalCeOi": MetricTag.DERIVED.value,
            "totalPeOi": MetricTag.DERIVED.value,
            "atmIv": MetricTag.DERIVED.value,
            "maxPain": MetricTag.DERIVED.value,
        },
    }

    return analytics


def _compute_max_pain(rows: list[dict]) -> Optional[float]:
    """Compute the max-pain strike for an option chain.

    Max pain is the strike price at which the aggregate open-interest-weighted
    monetary loss across all outstanding option contracts is minimised.  At
    expiry, calls lose intrinsic value for strikes below the settlement price
    and puts lose intrinsic value for strikes above the settlement price.

    For each candidate settlement strike S:
        loss(S) = Σ CE_OI[k] × max(k − S, 0)     (CE holders lose when S < k)
                + Σ PE_OI[k] × max(S − k, 0)     (PE holders lose when S > k)

    The candidate S that minimises loss(S) is max pain.

    Args:
        rows: Normalised option chain rows.

    Returns:
        Max-pain strike as a float, or None when insufficient data.
    """
    # Gather unique strikes
    strikes = sorted(
        {r["strike"] for r in rows if r.get("strike") is not None}
    )
    if not strikes:
        return None

    # Build OI maps per strike
    ce_oi: dict[float, int] = {}
    pe_oi: dict[float, int] = {}

    for r in rows:
        strike = r.get("strike")
        oi_val = r.get("oi")
        if strike is None or oi_val is None:
            continue
        opt_type = r.get("optionType", "")
        if opt_type in ("CE", "C"):
            ce_oi[strike] = ce_oi.get(strike, 0) + oi_val
        elif opt_type in ("PE", "P"):
            pe_oi[strike] = pe_oi.get(strike, 0) + oi_val

    if not ce_oi and not pe_oi:
        return None

    min_loss: Optional[float] = None
    max_pain_strike: Optional[float] = None

    for s in strikes:
        loss = 0.0
        # CE loss: CE holders at strike k lose when settlement S < k
        for k, oi_val in ce_oi.items():
            loss += oi_val * max(k - s, 0.0)
        # PE loss: PE holders at strike k lose when settlement S > k
        for k, oi_val in pe_oi.items():
            loss += oi_val * max(s - k, 0.0)

        if min_loss is None or loss < min_loss:
            min_loss = loss
            max_pain_strike = s

    return max_pain_strike


def _empty_analytics() -> dict:
    """Return an empty analytics dict for when no data is available."""
    return {
        "pcrOi": None,
        "pcrVolume": None,
        "maxCeOiStrike": None,
        "maxPeOiStrike": None,
        "totalCeOi": 0,
        "totalPeOi": 0,
        "atmIv": None,
        "maxPain": None,
        "_metricTags": {
            "pcrOi": MetricTag.DERIVED.value,
            "pcrVolume": MetricTag.DERIVED.value,
            "maxCeOiStrike": MetricTag.DERIVED.value,
            "maxPeOiStrike": MetricTag.DERIVED.value,
            "totalCeOi": MetricTag.DERIVED.value,
            "totalPeOi": MetricTag.DERIVED.value,
            "atmIv": MetricTag.DERIVED.value,
            "maxPain": MetricTag.DERIVED.value,
        },
    }


# ---------------------------------------------------------------------------
# Empty quote sentinel
# ---------------------------------------------------------------------------


def _empty_quote(
    instrument_id: str, exchange: str, market_status: str
) -> dict:
    """Return a minimal sentinel quote with all nullable fields set to None.

    Used when no cached quote is available during a non-REGULAR session.

    Args:
        instrument_id: Instrument identifier.
        exchange: Exchange identifier.
        market_status: Current market status string.

    Returns:
        Sentinel quote dict with all required fields present.
    """
    symbol = instrument_id.split(":")[-1] if ":" in instrument_id else instrument_id
    return {
        "instrumentId": instrument_id,
        "symbol": symbol,
        "exchange": exchange,
        "ltp": None,
        "open": None,
        "high": None,
        "low": None,
        "prevClose": None,
        "change": None,
        "changePct": None,
        "volume": 0,
        "volumeUnavailable": True,
        "oi": None,
        "oiMissing": True,
        "tradedValue": None,
        "totalBuyQty": None,
        "totalSellQty": None,
        "upperCircuit": None,
        "lowerCircuit": None,
        "weekHigh52": None,
        "weekLow52": None,
        "lastTradeTime": None,
        "bid": None,
        "ask": None,
        "bidAskMissing": True,
        "marketStatus": market_status,
        "provider": "none",
        "normalisationVersion": _NORMALISATION_VERSION,
        "provenance": _build_provenance(instrument_id, "none"),
    }


# ---------------------------------------------------------------------------
# Provenance builder
# ---------------------------------------------------------------------------


def _build_provenance(instrument_id: str, provider: str) -> dict:
    """Build a minimal provenance dict for a market data observation.

    Args:
        instrument_id: Instrument identifier.
        provider: Provider identifier.

    Returns:
        Provenance dict conforming to the DataProvenance schema.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "dataObservationId": str(uuid.uuid4()),
        "source": provider,
        "sourceVersion": "1.0",
        "eventTimeMs": now_ms,
        "receivedAtMs": now_ms,
        "availableAtMs": now_ms,
        "normalisationVersion": _NORMALISATION_VERSION,
        "validationApplied": True,
        "isFallback": False,
        "fallbackReason": None,
        "sourceChain": [provider],
    }
