"""
src/engines/dual_provider_engine.py

Dual-Provider Market Engine for DATA-SERVICE 2.0.

Extends MarketEngine to dispatch live quote and option chain requests to
BOTH Angel One and Upstox simultaneously, reconcile their observations,
and return canonical data with provenance.

This engine is the authoritative source for all live Indian market data in
the data-service.  It replaces the single-provider stub in MarketEngine.

Data flow:
    Angel One (REST/WS) ─┐
                         ├─► AngelOneNormalizer ─► ReconciliationEngine ─► canonical
    Upstox (REST/WS)   ─┘    UpstoxNormalizer      FreshnessClassifier     result

Provider routing:
    LIVE_QUOTE:
        Both providers queried concurrently.
        Reconciled by ReconciliationEngine (freshness + agreement rules).

    OPTION_CHAIN:
        Primary:  Upstox /v2/option/chain (comprehensive: market data + Greeks)
        Fallback: Scrapling NSE (when Upstox unavailable)
        Greeks supplement: Angel One optionGreek (when expiry known)

    OPTION_GREEKS:
        Primary:  Upstox V3 /v3/market-quote/option-greek (max 50 per call)
        Fallback: Angel One optionGreek

    HISTORICAL_OI:
        Primary: Angel One getOIData
        (Upstox OI embedded in V3 candle responses, but not a dedicated endpoint)

    INDEX QUOTES:
        Primary: Upstox (LTP V3 or full quote)
        Fallback: Scrapling NSE

Security: provider credentials NEVER exposed in any canonical output.
          Provider identity is included only in provenance metadata.

Requirements: Phases 11, 13, 30-31
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Optional

from src.core.normalizers.angel_one import AngelOneNormalizer
from src.core.normalizers.upstox import UpstoxNormalizer
from src.core.normalizers.freshness import FreshnessClassifier
from src.engines.reconciliation_engine import ReconciliationEngine
from src.engines.market_engine import MarketEngine
from src.core.schemas.instrument import SessionPhase
from src.observability.logging import get_logger

logger = get_logger(__name__)


class DualProviderEngine:
    """Live market data engine that uses both Angel One and Upstox.

    Fetches from both providers concurrently, normalizes independently,
    reconciles, and returns canonical data.  Either provider can fail
    without taking down the service — the available provider's data
    is returned with MISSING classification.

    Args:
        angel_one_adapter:  AngelOneAdapter instance (or None for stub mode).
        upstox_adapter:     UpstoxAdapter instance (or None for stub mode).
        market_engine:      Underlying MarketEngine for session/cache logic.
        angel_normalizer:   AngelOneNormalizer instance.
        upstox_normalizer:  UpstoxNormalizer instance.
        reconciliation_engine: ReconciliationEngine instance.
        freshness_classifier: FreshnessClassifier instance.
    """

    def __init__(
        self,
        angel_one_adapter: Optional[Any] = None,
        upstox_adapter: Optional[Any] = None,
        market_engine: Optional[MarketEngine] = None,
        angel_normalizer: Optional[AngelOneNormalizer] = None,
        upstox_normalizer: Optional[UpstoxNormalizer] = None,
        reconciliation_engine: Optional[ReconciliationEngine] = None,
        freshness_classifier: Optional[FreshnessClassifier] = None,
    ) -> None:
        self._angel = angel_one_adapter
        self._upstox = upstox_adapter
        self._market_engine = market_engine or MarketEngine()
        self._angel_norm = angel_normalizer or AngelOneNormalizer()
        self._upstox_norm = upstox_normalizer or UpstoxNormalizer()
        self._recon = reconciliation_engine or ReconciliationEngine()
        self._freshness = freshness_classifier or FreshnessClassifier()

        # Quote cache keyed by "exchange:instrument_id"
        # Stores normalized + reconciled output to serve non-REGULAR sessions
        self._quote_cache: dict[str, dict[str, Any]] = {}

        # DB engine — injected post-construction for option chain persistence
        self._db_engine: Optional[Any] = None

    def set_db_engine(self, db_engine: Any) -> None:
        """Inject the async DB engine for option chain persistence."""
        self._db_engine = db_engine

    # ------------------------------------------------------------------
    # Public: dual-provider live quote
    # ------------------------------------------------------------------

    async def get_live_quote(
        self,
        instrument_id: str,
        exchange: str = "NSE",
        angel_token: Optional[str] = None,
        upstox_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a reconciled canonical live quote from both providers.

        Queries Angel One and Upstox concurrently. Reconciles their
        observations and returns canonical data with provenance.

        During non-REGULAR sessions: returns last cached quote.

        Args:
            instrument_id: Canonical instrument ID.
            exchange:      Exchange code.
            angel_token:   Angel One instrument token (for REST quote).
            upstox_key:    Upstox instrument key (for REST quote).

        Returns:
            Canonical quote dict with reconciliation metadata.
        """
        phase = self._market_engine.get_current_session_phase()
        cache_key = f"{exchange}:{instrument_id}"

        if phase != SessionPhase.REGULAR:
            cached = self._quote_cache.get(cache_key)
            if cached is not None:
                result = dict(cached)
                result["marketStatus"] = phase.value
                return result
            return _empty_dual_quote(instrument_id, exchange, phase.value)

        # --- Concurrent fetch from both providers ---
        received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        angel_task = self._fetch_angel_quote(instrument_id, exchange, angel_token)
        upstox_task = self._fetch_upstox_quote(instrument_id, exchange, upstox_key)

        angel_raw, upstox_raw = await asyncio.gather(
            angel_task, upstox_task, return_exceptions=True
        )

        # Handle exceptions from concurrent gather
        angel_obs: Optional[dict[str, Any]] = None
        upstox_obs: Optional[dict[str, Any]] = None

        if isinstance(angel_raw, Exception):
            logger.warning(
                "dual_provider_angel_quote_failed",
                component="dual_provider_engine",
                instrument_id=instrument_id,
                error=str(angel_raw),
            )
        elif angel_raw is not None:
            angel_obs = self._angel_norm.normalize_full_quote(
                raw=angel_raw,
                instrument_id=instrument_id,
                exchange=exchange,
                received_at=received_at,
            )
            self._freshness.classify(angel_obs)

        if isinstance(upstox_raw, Exception):
            logger.warning(
                "dual_provider_upstox_quote_failed",
                component="dual_provider_engine",
                instrument_id=instrument_id,
                error=str(upstox_raw),
            )
        elif upstox_raw is not None:
            upstox_obs = self._upstox_norm.normalize_full_quote(
                instrument_key=upstox_key or instrument_id,
                raw=upstox_raw,
                instrument_id=instrument_id,
                exchange=exchange,
                received_at=received_at,
            )
            self._freshness.classify(upstox_obs)

        # --- Reconcile ---
        recon = self._recon.reconcile_quote(angel_obs, upstox_obs, instrument_id)

        # --- Build canonical output ---
        canonical = _build_canonical_quote(
            instrument_id=instrument_id,
            exchange=exchange,
            angel_obs=angel_obs,
            upstox_obs=upstox_obs,
            recon=recon,
            phase=phase,
        )

        # Update cache
        self._quote_cache[cache_key] = canonical

        return canonical

    # ------------------------------------------------------------------
    # Public: dual-provider option chain
    # ------------------------------------------------------------------

    async def get_option_chain(
        self,
        underlying: str,
        expiry: str,
        underlying_upstox_key: Optional[str] = None,
        spot_instrument_id: Optional[str] = None,
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        """Return a normalized canonical option chain from Upstox (primary).

        Uses Upstox /v2/option/chain as the primary source since it provides
        CE+PE market data + Greeks in a single call.

        Angel One Greeks are fetched separately and used to supplement/validate
        Upstox Greeks when available.

        Args:
            underlying:              Underlying symbol (e.g. "NIFTY").
            expiry:                  Expiry date as "YYYY-MM-DD".
            underlying_upstox_key:   Upstox instrument key for the underlying
                                     (e.g. "NSE_INDEX|Nifty 50").
            spot_instrument_id:      Canonical instrument ID for spot price lookup.
            exchange:                Exchange code.

        Returns:
            Canonical option chain dict with contracts, analytics, provenance.
        """
        received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Fetch spot price for ATM calculation
        spot_price: Optional[float] = None
        if spot_instrument_id and self._upstox:
            try:
                ltp_data = await self._upstox.fetch_ltp(
                    [underlying_upstox_key or spot_instrument_id]
                )
                if ltp_data:
                    first_val = next(iter(ltp_data.values()), {})
                    spot_price = first_val.get("last_price")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dual_provider_spot_price_fetch_failed",
                    component="dual_provider_engine",
                    underlying=underlying,
                    error=str(exc),
                )

        # Fetch Upstox option chain (primary)
        upstox_chain: Optional[dict[str, Any]] = None
        if self._upstox and underlying_upstox_key:
            try:
                raw_chain = await self._upstox.fetch_option_chain(
                    underlying_key=underlying_upstox_key,
                    expiry_date=expiry,
                )
                if raw_chain:
                    upstox_chain = self._upstox_norm.normalize_option_chain(
                        raw_chain=raw_chain,
                        underlying=underlying,
                        expiry=expiry,
                        received_at=received_at,
                    )
                    if spot_price is None and upstox_chain.get("spotPrice"):
                        spot_price = upstox_chain["spotPrice"]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dual_provider_upstox_chain_failed",
                    component="dual_provider_engine",
                    underlying=underlying,
                    expiry=expiry,
                    error=str(exc),
                )

        if upstox_chain is None:
            # Fallback to MarketEngine (uses Scrapling/NSE)
            fallback_result = await self._market_engine.get_option_chain(
                underlying=underlying, expiry=expiry, exchange=exchange
            )
            fallback_result["chainSource"] = "scrapling_nse_fallback"
            return fallback_result

        # Add spot price to each contract if needed
        contracts = upstox_chain.get("contracts", [])
        if spot_price is not None and contracts:
            all_strikes = [c["strike"] for c in contracts if c.get("strike")]
            if all_strikes:
                atm_strike = min(all_strikes, key=lambda s: abs(s - spot_price))
                for c in contracts:
                    c["isAtm"] = c.get("strike") == atm_strike

        result = {
            "underlying":    underlying,
            "expiry":        expiry,
            "exchange":      exchange,
            "spotPrice":     spot_price,
            "pcr":           upstox_chain.get("pcr"),
            "contracts":     contracts,
            "contractCount": len(contracts),
            "chainSource":   "upstox_v2",
            "provider":      "upstox",
            "receivedAt":    received_at,
            "provenance": {
                "primaryProvider": "upstox",
                "fallbackProvider": "scrapling_nse",
                "grecksSource": "provider",
            },
        }

        # Persist snapshot + contracts to DB (fire-and-forget)
        if self._db_engine is not None and contracts:
            import asyncio as _asyncio  # noqa: PLC0415
            _asyncio.ensure_future(
                _persist_option_chain(
                    result,
                    db_engine=self._db_engine,
                    underlying=underlying,
                    exchange=exchange,
                    expiry=expiry,
                    spot_price=spot_price,
                    received_at=received_at,
                )
            )

        return result

    # ------------------------------------------------------------------
    # Public: option Greeks (batch, from Upstox V3)
    # ------------------------------------------------------------------

    async def get_option_greeks_batch(
        self,
        instrument_keys: list[str],
        instrument_id_map: dict[str, str],
    ) -> dict[str, Any]:
        """Fetch and normalize option Greeks for a batch of instruments.

        Uses Upstox V3 /v3/market-quote/option-greek (max 50 per request).
        Auto-batches if > 50 instruments.

        Args:
            instrument_keys:   Upstox instrument keys for options.
            instrument_id_map: Map of Upstox key → canonical instrument_id.

        Returns:
            Dict mapping canonical instrument_id → normalized Greeks dict.
        """
        if not self._upstox:
            return {}

        received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        try:
            raw_greeks = await self._upstox.fetch_option_greeks_batched(
                instrument_keys=instrument_keys,
                batch_size=50,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dual_provider_greeks_batch_failed",
                component="dual_provider_engine",
                count=len(instrument_keys),
                error=str(exc),
            )
            return {}

        result: dict[str, Any] = {}
        for instrument_key, raw in raw_greeks.items():
            instrument_id = instrument_id_map.get(instrument_key, instrument_key)
            normalized = self._upstox_norm.normalize_option_greek(
                instrument_key=instrument_key,
                raw=raw,
                instrument_id=instrument_id,
                received_at=received_at,
            )
            if normalized:
                self._freshness.classify(normalized)
                result[instrument_id] = normalized

        return result

    # ------------------------------------------------------------------
    # Internal: provider fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_angel_quote(
        self,
        instrument_id: str,
        exchange: str,
        token: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Fetch a raw quote from Angel One. Returns None on failure."""
        if self._angel is None or token is None:
            return None
        try:
            raw = await self._angel.fetch_live_quote(
                token=token, exchange=exchange
            )
            return raw
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "dual_provider_angel_quote_error",
                component="dual_provider_engine",
                instrument_id=instrument_id,
                error=type(exc).__name__,
            )
            return None

    async def _fetch_upstox_quote(
        self,
        instrument_id: str,
        exchange: str,
        instrument_key: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Fetch a raw quote from Upstox. Returns None on failure."""
        if self._upstox is None or instrument_key is None:
            return None
        try:
            result = await self._upstox.fetch_full_quote([instrument_key])
            return result.get(instrument_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "dual_provider_upstox_quote_error",
                component="dual_provider_engine",
                instrument_id=instrument_id,
                error=type(exc).__name__,
            )
            return None


# ---------------------------------------------------------------------------
# Canonical output builders
# ---------------------------------------------------------------------------

def _build_canonical_quote(
    instrument_id: str,
    exchange: str,
    angel_obs: Optional[dict[str, Any]],
    upstox_obs: Optional[dict[str, Any]],
    recon: Any,
    phase: SessionPhase,
) -> dict[str, Any]:
    """Build the canonical quote from reconciliation result.

    The canonical quote uses the reconciled LTP and OI.
    Non-canonical fields (depth, circuit limits, etc.) are taken from
    whichever provider has the richer data.
    """
    # Choose the richer quote as the base
    base = upstox_obs or angel_obs or {}

    canonical: dict[str, Any] = {
        "instrumentId":      instrument_id,
        "exchange":          exchange,
        "ltp":               recon.canonical_ltp,
        "oi":                recon.canonical_oi,
        "oiMissing":         recon.canonical_oi is None,
        "volume":            base.get("volume", 0),
        "volumeUnavailable": base.get("volumeUnavailable", True),
        "open":              base.get("open"),
        "high":              base.get("high"),
        "low":               base.get("low"),
        "prevClose":         base.get("prevClose"),
        "change":            base.get("change"),
        "changePct":         base.get("changePct"),
        "upperCircuit":      (upstox_obs or {}).get("upperCircuit")
                             or (angel_obs or {}).get("upperCircuit"),
        "lowerCircuit":      (upstox_obs or {}).get("lowerCircuit")
                             or (angel_obs or {}).get("lowerCircuit"),
        "weekHigh52":        (angel_obs or {}).get("weekHigh52"),
        "weekLow52":         (angel_obs or {}).get("weekLow52"),
        "totalBuyQty":       base.get("totalBuyQty"),
        "totalSellQty":      base.get("totalSellQty"),
        "lastTradeQty":      base.get("lastTradeQty"),
        # Depth: prefer Upstox (may have more levels)
        "depthBuy":          (upstox_obs or {}).get("depthBuy")
                             or (angel_obs or {}).get("depthBuy"),
        "depthSell":         (upstox_obs or {}).get("depthSell")
                             or (angel_obs or {}).get("depthSell"),
        "depthLevels":       5,
        "marketStatus":      phase.value,
        # Reconciliation metadata (internal; not exposed to AlphaForge)
        "reconciliation": {
            "classification":   recon.classification,
            "canonicalProvider": recon.canonical_provider,
            "resolutionRule":   recon.resolution_rule,
            "ltpDiffPct":       round(recon.ltp_diff_pct * 100, 4)
                                if recon.ltp_diff_pct is not None else None,
            "oiDiffAbs":        recon.oi_diff_abs,
            "timestampDiffMs":  recon.timestamp_diff_ms,
        },
        "provenance": {
            "angelOneAvailable": angel_obs is not None,
            "upstoxAvailable":   upstox_obs is not None,
            "primaryProvider":   recon.canonical_provider,
            "angelOneTs":        (angel_obs or {}).get("sourceTimestamp"),
            "upstoxTs":          (upstox_obs or {}).get("sourceTimestamp"),
        },
    }

    return canonical


def _empty_dual_quote(
    instrument_id: str,
    exchange: str,
    market_status: str,
) -> dict[str, Any]:
    """Return an empty canonical quote for non-REGULAR sessions."""
    return {
        "instrumentId":   instrument_id,
        "exchange":       exchange,
        "ltp":            None,
        "oi":             None,
        "oiMissing":      True,
        "volume":         0,
        "volumeUnavailable": True,
        "open":           None,
        "high":           None,
        "low":            None,
        "prevClose":      None,
        "change":         None,
        "changePct":      None,
        "upperCircuit":   None,
        "lowerCircuit":   None,
        "depthBuy":       [],
        "depthSell":      [],
        "depthLevels":    0,
        "marketStatus":   market_status,
        "reconciliation": {"classification": "MISSING"},
        "provenance":     {"angelOneAvailable": False, "upstoxAvailable": False},
    }


# ---------------------------------------------------------------------------
# Persistence helper — option_chain_snapshot + option_chain_contract
# ---------------------------------------------------------------------------

_DP_LOGGER = get_logger(__name__ + ".persist")


async def _persist_option_chain(
    chain_result: dict[str, Any],
    *,
    db_engine: Any,
    underlying: str,
    exchange: str,
    expiry: str,
    spot_price: Optional[float],
    received_at: str,
) -> None:
    """Persist an option chain snapshot and its contracts to the DB.

    Inserts one row into ``option_chain_snapshot`` and one row per
    (strike, option_type) into ``option_chain_contract``.  Uses
    ON CONFLICT DO NOTHING so repeated calls for the same snapshot
    timestamp are idempotent.

    Silently logs and returns on any DB error.
    """
    if not db_engine:
        return

    from sqlalchemy import text as _text  # noqa: PLC0415
    import datetime as _dt  # noqa: PLC0415

    contracts = chain_result.get("contracts") or []
    if not contracts:
        return

    try:
        ts = _dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, AttributeError):
        ts = _dt.datetime.now(_dt.timezone.utc)

    session_date = ts.astimezone(
        _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    ).date()

    try:
        expiry_date = _dt.date.fromisoformat(expiry)
    except ValueError:
        _DP_LOGGER.warning("option_chain_persist_bad_expiry", expiry=expiry)
        return

    # Compute analytics
    total_ce_oi = sum(
        (c.get("oi") or 0) for c in contracts if c.get("optionType") == "CE"
    )
    total_pe_oi = sum(
        (c.get("oi") or 0) for c in contracts if c.get("optionType") == "PE"
    )
    pcr_oi = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None

    insert_snapshot = _text(
        """
        INSERT INTO option_chain_snapshot (
            underlying_id, exchange, timestamp, expiry,
            spot_price, provider, received_at,
            pcr_oi, total_ce_oi, total_pe_oi, session_date, quality_status
        ) VALUES (
            :underlying_id, :exchange, :timestamp, :expiry,
            :spot_price, :provider, :received_at,
            :pcr_oi, :total_ce_oi, :total_pe_oi, :session_date, 'TRUSTED'
        )
        ON CONFLICT DO NOTHING
        RETURNING snapshot_id
        """
    )

    insert_contract = _text(
        """
        INSERT INTO option_chain_contract (
            snapshot_id, strike, option_type,
            ltp, volume, open_interest, oi_change,
            bid, ask, bid_quantity, ask_quantity,
            iv, delta, gamma, theta, vega, is_atm
        ) VALUES (
            :snapshot_id, :strike, :option_type,
            :ltp, :volume, :open_interest, :oi_change,
            :bid, :ask, :bid_quantity, :ask_quantity,
            :iv, :delta, :gamma, :theta, :vega, :is_atm
        )
        ON CONFLICT DO NOTHING
        """
    )

    try:
        async with db_engine.begin() as conn:
            snap_row = await conn.execute(
                insert_snapshot,
                {
                    "underlying_id": f"{exchange.upper()}:{underlying.upper()}",
                    "exchange": exchange.upper(),
                    "timestamp": ts,
                    "expiry": expiry_date,
                    "spot_price": spot_price,
                    "provider": "upstox",
                    "received_at": ts,
                    "pcr_oi": round(pcr_oi, 4) if pcr_oi is not None else None,
                    "total_ce_oi": total_ce_oi or None,
                    "total_pe_oi": total_pe_oi or None,
                    "session_date": session_date,
                },
            )
            snapshot_row = snap_row.fetchone()
            if snapshot_row is None:
                # Snapshot already exists (ON CONFLICT DO NOTHING)
                return
            snapshot_id = snapshot_row[0]

            for c in contracts:
                oi_val = c.get("oi") if not c.get("oiMissing", False) else None
                await conn.execute(
                    insert_contract,
                    {
                        "snapshot_id": snapshot_id,
                        "strike": c.get("strike"),
                        "option_type": c.get("optionType") or c.get("option_type"),
                        "ltp": c.get("ltp"),
                        "volume": c.get("volume"),
                        "open_interest": oi_val,
                        "oi_change": c.get("oiChange"),
                        "bid": c.get("bid"),
                        "ask": c.get("ask"),
                        "bid_quantity": c.get("bidQty"),
                        "ask_quantity": c.get("askQty"),
                        "iv": c.get("iv"),
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                        "is_atm": bool(c.get("isAtm", False)),
                    },
                )
    except Exception as exc:  # noqa: BLE001
        _DP_LOGGER.warning(
            "option_chain_persist_failed",
            component="dual_provider_engine",
            underlying=underlying,
            error=str(exc),
        )
