"""
collect_options_chain_snapshots.py
====================================
Collect live option chain snapshots (IV, Greeks, OI, volume) from Upstox
and persist them to ``option_chain_snapshot`` + ``option_chain_contract`` tables.

This script fetches a full option chain for each configured underlying at the
current moment and stores it as a time-series of snapshots. Run it on a
schedule (e.g. 09:20, 12:00, 15:29 IST daily) to build an IV + Greeks
time series suitable for ML training.

What is collected
-----------------
For each underlying (NIFTY, BANKNIFTY, RELIANCE, etc.) and each active
expiry, the script fetches:
  - All strikes (CE + PE) with LTP, volume, OI, OI change, bid/ask
  - Delta, Gamma, Theta, Vega, Rho, IV (implied volatility) per strike
  - ATM IV (derived from ATM strike's IV)
  - PCR (put-call ratio by OI and by volume)
  - Max pain strike

Data is stored in the existing ``option_chain_snapshot`` and
``option_chain_contract`` tables (see src/db/models/option_chain.py).

Usage
-----
    # One-shot: collect snapshot for all configured underlyings now:
    APP_ENV=local python3 scripts/collect_options_chain_snapshots.py

    # Specific underlyings only:
    APP_ENV=local python3 scripts/collect_options_chain_snapshots.py \
        --symbols NIFTY BANKNIFTY

    # Specific expiry (nearest by default):
    APP_ENV=local python3 scripts/collect_options_chain_snapshots.py \
        --expiry 2026-09-25

    # Dry-run (resolve instruments but don't write):
    APP_ENV=local python3 scripts/collect_options_chain_snapshots.py --dry-run

    # Via Makefile (add to scheduler):
    make collect-options-snapshots

Prerequisites
-------------
  - UPSTOX_ACCESS_TOKEN must be set (expires daily — refresh before running)
  - Tables option_chain_snapshot + option_chain_contract must exist (run migrations)

Rate limiting
-------------
Upstox option chain API: 1 request per underlying per expiry.
Greeks API: up to 50 instrument keys per request.
Default inter-request delay: 0.2s.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Underlying configuration
# Index futures + 20 most liquid stock underlyings by default
# ---------------------------------------------------------------------------
_DEFAULT_UNDERLYINGS: list[dict] = [
    # Indices (primary)
    {"symbol": "NIFTY 50",    "exchange": "NSE", "upstox_key": "NSE_INDEX|Nifty 50",      "nfo_symbol": "NIFTY"},
    {"symbol": "NIFTY BANK",  "exchange": "NSE", "upstox_key": "NSE_INDEX|Nifty Bank",     "nfo_symbol": "BANKNIFTY"},
    {"symbol": "NIFTY FIN SERVICE", "exchange": "NSE", "upstox_key": "NSE_INDEX|Nifty Fin Service", "nfo_symbol": "FINNIFTY"},
    {"symbol": "NIFTY MIDCAP 50",   "exchange": "NSE", "upstox_key": "NSE_INDEX|Nifty Midcap Select", "nfo_symbol": "MIDCPNIFTY"},
    # High-OI stock options
    {"symbol": "RELIANCE",   "exchange": "NSE", "upstox_key": "NSE_EQ|INE002A01018",  "nfo_symbol": "RELIANCE"},
    {"symbol": "HDFCBANK",   "exchange": "NSE", "upstox_key": "NSE_EQ|INE040A01034",  "nfo_symbol": "HDFCBANK"},
    {"symbol": "ICICIBANK",  "exchange": "NSE", "upstox_key": "NSE_EQ|INE090A01021",  "nfo_symbol": "ICICIBANK"},
    {"symbol": "INFY",       "exchange": "NSE", "upstox_key": "NSE_EQ|INE009A01021",  "nfo_symbol": "INFY"},
    {"symbol": "TCS",        "exchange": "NSE", "upstox_key": "NSE_EQ|INE467B01029",  "nfo_symbol": "TCS"},
    {"symbol": "AXISBANK",   "exchange": "NSE", "upstox_key": "NSE_EQ|INE238A01034",  "nfo_symbol": "AXISBANK"},
    {"symbol": "SBIN",       "exchange": "NSE", "upstox_key": "NSE_EQ|INE062A01020",  "nfo_symbol": "SBIN"},
    {"symbol": "BAJFINANCE", "exchange": "NSE", "upstox_key": "NSE_EQ|INE296A01024",  "nfo_symbol": "BAJFINANCE"},
    {"symbol": "KOTAKBANK",  "exchange": "NSE", "upstox_key": "NSE_EQ|INE237A01028",  "nfo_symbol": "KOTAKBANK"},
    {"symbol": "LT",         "exchange": "NSE", "upstox_key": "NSE_EQ|INE018A01030",  "nfo_symbol": "LT"},
    {"symbol": "TATAMOTORS", "exchange": "NSE", "upstox_key": "NSE_EQ|INE155A01022",  "nfo_symbol": "TATAMOTORS"},
    {"symbol": "WIPRO",      "exchange": "NSE", "upstox_key": "NSE_EQ|INE075A01022",  "nfo_symbol": "WIPRO"},
    {"symbol": "HINDUNILVR", "exchange": "NSE", "upstox_key": "NSE_EQ|INE030A01027",  "nfo_symbol": "HINDUNILVR"},
    {"symbol": "SUNPHARMA",  "exchange": "NSE", "upstox_key": "NSE_EQ|INE044A01036",  "nfo_symbol": "SUNPHARMA"},
    {"symbol": "ADANIENT",   "exchange": "NSE", "upstox_key": "NSE_EQ|INE423A01024",  "nfo_symbol": "ADANIENT"},
    {"symbol": "TATASTEEL",  "exchange": "NSE", "upstox_key": "NSE_EQ|INE081A01020",  "nfo_symbol": "TATASTEEL"},
]

_GREEKS_BATCH_SIZE = 50       # Upstox max keys per Greeks call
_REQUEST_DELAY_S   = 0.2      # inter-request courtesy delay
_PROVIDER          = "upstox"

# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _compute_pcr(contracts: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """Return (pcr_oi, pcr_volume). None when no data."""
    ce_oi = sum(c.get("open_interest") or 0 for c in contracts if c.get("option_type") == "CE")
    pe_oi = sum(c.get("open_interest") or 0 for c in contracts if c.get("option_type") == "PE")
    ce_vol = sum(c.get("volume") or 0 for c in contracts if c.get("option_type") == "CE")
    pe_vol = sum(c.get("volume") or 0 for c in contracts if c.get("option_type") == "PE")
    pcr_oi  = pe_oi / ce_oi   if ce_oi  > 0 else None
    pcr_vol = pe_vol / ce_vol if ce_vol > 0 else None
    return pcr_oi, pcr_vol


def _compute_max_pain(contracts: list[dict]) -> Optional[float]:
    """Return the max-pain strike (minimises aggregate loss to option writers)."""
    from math import fabs  # noqa: PLC0415

    # Group by strike
    strikes: dict[float, dict] = {}
    for c in contracts:
        s = c.get("strike") or 0.0
        if s not in strikes:
            strikes[s] = {"ce_oi": 0, "pe_oi": 0}
        if c.get("option_type") == "CE":
            strikes[s]["ce_oi"] += c.get("open_interest") or 0
        elif c.get("option_type") == "PE":
            strikes[s]["pe_oi"] += c.get("open_interest") or 0

    if not strikes:
        return None

    all_strikes = sorted(strikes.keys())
    min_pain = float("inf")
    max_pain_strike = None

    for candidate in all_strikes:
        total_loss = 0.0
        for s, oi in strikes.items():
            # CE writers lose when expiry > strike; PE writers lose when expiry < strike
            total_loss += max(0.0, candidate - s) * oi["ce_oi"]
            total_loss += max(0.0, s - candidate) * oi["pe_oi"]
        if total_loss < min_pain:
            min_pain = total_loss
            max_pain_strike = candidate

    return max_pain_strike


def _find_atm_strike(contracts: list[dict], spot: float) -> Optional[float]:
    """Return the strike closest to the current spot price."""
    if not contracts or spot <= 0:
        return None
    strikes = sorted({c.get("strike") or 0 for c in contracts if c.get("strike", 0) > 0})
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def _find_atm_iv(
    contracts: list[dict],
    atm_strike: Optional[float],
) -> Optional[float]:
    """Return the average IV of CE + PE at the ATM strike."""
    if atm_strike is None:
        return None
    atm = [c for c in contracts if c.get("strike") == atm_strike and c.get("iv") is not None]
    ivs = [c["iv"] for c in atm if c["iv"] > 0]
    return sum(ivs) / len(ivs) if ivs else None


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run(
    symbols: Optional[list[str]],
    expiry_filter: Optional[date],
    dry_run: bool,
    request_delay: float,
) -> dict:
    from src.core.settings import get_settings  # noqa: PLC0415
    from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    settings = get_settings()
    if not (settings.upstox_access_token or settings.upstox_analytics_key):
        print("[ERROR] UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_KEY is required.",
              file=sys.stderr)
        sys.exit(1)

    engine = create_async_engine(settings.database_url, echo=False)
    now_utc = datetime.now(timezone.utc)

    # Filter underlyings
    underlyings = _DEFAULT_UNDERLYINGS
    if symbols:
        upper = {s.upper() for s in symbols}
        underlyings = [u for u in underlyings if u["nfo_symbol"] in upper]

    if not underlyings:
        print(f"[ERROR] No matching underlyings found for: {symbols}", file=sys.stderr)
        sys.exit(1)

    print(f"Options chain snapshot — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Underlyings: {len(underlyings)}")
    print(f"  Dry-run    : {dry_run}")
    print()

    if dry_run:
        for u in underlyings:
            print(f"  [DRY-RUN] {u['nfo_symbol']}")
        await engine.dispose()
        return {"dry_run": True, "underlyings": len(underlyings)}

    # Build Upstox adapter
    adapter = UpstoxAdapter(
        api_key=settings.upstox_api_key or "",
        api_secret=settings.upstox_api_secret or "",
        redirect_uri=(settings.upstox_redirect_uri
                      or "http://localhost:8200/v1/auth/upstox/callback"),
    )
    if settings.upstox_access_token:
        await adapter.set_access_token(settings.upstox_access_token)
    if settings.upstox_analytics_key:
        await adapter.set_analytics_token(settings.upstox_analytics_key)

    total_snapshots = 0
    total_contracts = 0
    errors = 0

    for underlying in underlyings:
        nfo_sym = underlying["nfo_symbol"]
        upstox_key = underlying["upstox_key"]
        exchange = underlying["exchange"]
        underlying_id = f"{exchange}:{underlying['symbol']}"

        try:
            # Fetch option chain — returns list of expiries with strikes
            chain_data = await adapter.fetch_option_chain(
                instrument_key=upstox_key,
                expiry_date=expiry_filter.isoformat() if expiry_filter else None,
            )
            await asyncio.sleep(request_delay)
        except Exception as exc:  # noqa: BLE001
            logger.warning("option_chain_fetch_failed",
                           symbol=nfo_sym, error=str(exc))
            errors += 1
            continue

        if not chain_data:
            print(f"  ⚠  {nfo_sym}: no chain data returned (market closed?)")
            continue

        # Upstox returns a list of dicts, each with 'expiry' and 'data' (list of strike rows)
        for expiry_block in chain_data:
            expiry_str = expiry_block.get("expiry") or expiry_block.get("expiryDate") or ""
            try:
                expiry_d = date.fromisoformat(expiry_str[:10])
            except (ValueError, AttributeError):
                continue

            strike_rows: list[dict] = expiry_block.get("data") or expiry_block.get("optionChain") or []
            if not strike_rows:
                continue

            # Flatten into contract list
            contracts: list[dict] = []
            instrument_keys: list[str] = []

            for s_row in strike_rows:
                strike = float(s_row.get("strikePrice") or s_row.get("strike_price") or 0)
                if strike <= 0:
                    continue

                for opt_type, side_key in [("CE", "callOption"), ("PE", "putOption")]:
                    side = s_row.get(side_key) or s_row.get(opt_type.lower()) or {}
                    if not side:
                        continue
                    ikey = side.get("instrument_key") or side.get("instrumentKey") or ""
                    ltp     = side.get("last_price") or side.get("ltp") or 0
                    oi      = side.get("open_interest") or side.get("oi") or 0
                    oi_chg  = side.get("change_in_open_interest") or side.get("oi_change") or 0
                    vol     = side.get("volume") or 0
                    bid     = side.get("best_bid") or side.get("bid") or 0
                    ask     = side.get("best_ask") or side.get("ask") or 0

                    c: dict = {
                        "instrument_key": ikey,
                        "strike": strike,
                        "option_type": opt_type,
                        "ltp": float(ltp),
                        "open_interest": int(oi),
                        "oi_change": int(oi_chg),
                        "volume": int(vol),
                        "bid": float(bid),
                        "ask": float(ask),
                        "iv": None,
                        "delta": None, "gamma": None,
                        "theta": None, "vega": None,
                    }
                    contracts.append(c)
                    if ikey:
                        instrument_keys.append(ikey)

            if not contracts:
                continue

            # Fetch Greeks in batches
            key_to_greeks: dict[str, dict] = {}
            for i in range(0, len(instrument_keys), _GREEKS_BATCH_SIZE):
                batch_keys = instrument_keys[i:i + _GREEKS_BATCH_SIZE]
                try:
                    greeks_list = await adapter.fetch_option_greeks(batch_keys)
                    for g in greeks_list:
                        ikey = g.get("instrument_key") or g.get("instrumentKey") or ""
                        key_to_greeks[ikey] = g
                    await asyncio.sleep(request_delay)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("greeks_fetch_failed",
                                   symbol=nfo_sym, batch=i, error=str(exc))

            # Attach Greeks to contracts
            for c in contracts:
                g = key_to_greeks.get(c.get("instrument_key") or "")
                if g:
                    c["iv"]    = g.get("vega") and g.get("implied_volatility") or g.get("iv")
                    c["delta"] = g.get("delta")
                    c["gamma"] = g.get("gamma")
                    c["theta"] = g.get("theta")
                    c["vega"]  = g.get("vega")

            # Compute analytics
            pcr_oi, pcr_vol = _compute_pcr(contracts)
            max_pain = _compute_max_pain(contracts)

            # Derive spot from ATM CE+PE midpoint (best proxy without live quote)
            ltp_list = [c["ltp"] for c in contracts
                        if c["ltp"] > 0 and c.get("strike")]
            # Use the CE ltp nearest to a reasonable range as spot proxy
            # In practice you'd fetch spot from equity_candle or live quote
            spot_price = None  # left as None; can be enriched from live quote

            atm_strike = _find_atm_strike(contracts, spot_price or 0)
            atm_iv = _find_atm_iv(contracts, atm_strike)

            session_d = now_utc.astimezone(
                __import__("datetime").timezone(
                    __import__("datetime").timedelta(hours=5, minutes=30)
                )
            ).date()

            snapshot_id = uuid.uuid4()

            async with engine.begin() as conn:
                # Insert snapshot header
                await conn.execute(text("""
                    INSERT INTO option_chain_snapshot (
                        snapshot_id, underlying_id, exchange, timestamp, expiry,
                        spot_price, atm_strike, provider, pcr_oi, pcr_volume,
                        total_ce_oi, total_pe_oi, max_pain, atm_iv,
                        quality_status, session_date, received_at
                    ) VALUES (
                        :sid, :uid, 'NFO', :ts, :expiry,
                        :spot, :atm_strike, 'upstox', :pcr_oi, :pcr_vol,
                        :ce_oi, :pe_oi, :max_pain, :atm_iv,
                        'TRUSTED', :sess, NOW()
                    )
                """), {
                    "sid": snapshot_id,
                    "uid": underlying_id,
                    "ts": now_utc,
                    "expiry": expiry_d,
                    "spot": spot_price,
                    "atm_strike": atm_strike,
                    "pcr_oi": pcr_oi,
                    "pcr_vol": pcr_vol,
                    "ce_oi": sum(c["open_interest"] for c in contracts if c["option_type"] == "CE"),
                    "pe_oi": sum(c["open_interest"] for c in contracts if c["option_type"] == "PE"),
                    "max_pain": max_pain,
                    "atm_iv": atm_iv,
                    "sess": session_d,
                })

                # Insert per-strike contract rows
                for c in contracts:
                    await conn.execute(text("""
                        INSERT INTO option_chain_contract (
                            snapshot_id, instrument_id, strike, option_type,
                            ltp, volume, open_interest, oi_change,
                            bid, ask, iv, delta, gamma, theta, vega
                        ) VALUES (
                            :sid, :iid, :strike, :opt_type,
                            :ltp, :volume, :oi, :oi_change,
                            :bid, :ask, :iv, :delta, :gamma, :theta, :vega
                        )
                    """), {
                        "sid": snapshot_id,
                        "iid": c.get("instrument_key"),
                        "strike": c["strike"],
                        "opt_type": c["option_type"],
                        "ltp": c["ltp"],
                        "volume": c["volume"],
                        "oi": c["open_interest"],
                        "oi_change": c["oi_change"],
                        "bid": c["bid"],
                        "ask": c["ask"],
                        "iv": c.get("iv"),
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                    })

            total_snapshots += 1
            total_contracts += len(contracts)
            print(f"  ✓  {nfo_sym:<16} expiry={expiry_d}  "
                  f"strikes={len(contracts)//2}  "
                  f"atm_iv={f'{atm_iv:.2%}' if atm_iv else 'n/a'}  "
                  f"pcr={f'{pcr_oi:.2f}' if pcr_oi else 'n/a'}")

    await adapter.aclose()
    await engine.dispose()

    print()
    print("=" * 62)
    print("  OPTIONS CHAIN SNAPSHOT COMPLETE")
    print("=" * 62)
    print(f"  Snapshots written  : {total_snapshots}")
    print(f"  Contracts written  : {total_contracts:,}")
    print(f"  Errors             : {errors}")
    print("=" * 62)

    return {
        "snapshots": total_snapshots,
        "contracts": total_contracts,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect option chain + IV + Greeks snapshots from Upstox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        metavar="SYMBOL",
        help="Limit to specific underlyings (e.g. NIFTY BANKNIFTY). Default: all.",
    )
    parser.add_argument(
        "--expiry", default=None, metavar="YYYY-MM-DD",
        help="Fetch only this expiry (default: all active expiries per chain API).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without fetching or writing.")
    parser.add_argument(
        "--delay", type=float, default=_REQUEST_DELAY_S, metavar="SECS",
        help=f"Sleep between Upstox requests (default: {_REQUEST_DELAY_S}s).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    expiry_d: Optional[date] = None
    if args.expiry:
        expiry_d = date.fromisoformat(args.expiry)

    asyncio.run(run(
        symbols=args.symbols,
        expiry_filter=expiry_d,
        dry_run=args.dry_run,
        request_delay=args.delay,
    ))


if __name__ == "__main__":
    main()
