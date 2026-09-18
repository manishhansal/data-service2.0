"""
Live provider verification script.
Run inside container: docker exec data-service-api python3 /app/scripts/_live_verify.py
Run outside container: python3 scripts/_live_verify.py
"""
import asyncio
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import dotenv_values
    env = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.local"))
    for k, v in env.items():
        if v:
            os.environ.setdefault(k, v)
except Exception:
    pass  # running inside container with env already set


def C(v):
    return "CONFIGURED" if v else "MISSING"


async def run():
    from src.core.settings import get_settings
    s = get_settings()

    print("=" * 60)
    print("LIVE PROVIDER VERIFICATION")
    print(f"Time (UTC): {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("=" * 60)

    print("\n=== CREDENTIAL STATUS ===")
    print(f"ANGEL_ONE_API_KEY     : {C(s.angel_one_api_key)}")
    print(f"ANGEL_ONE_CLIENT_ID   : {C(s.angel_one_client_id)}")
    print(f"ANGEL_ONE_TOTP_SECRET : {C(s.angel_one_totp_secret)}")
    print(f"ANGEL_ONE_MPIN        : {C(s.angel_one_mpin)}")
    print(f"UPSTOX_API_KEY        : {C(s.upstox_api_key)}")
    print(f"UPSTOX_API_SECRET     : {C(s.upstox_api_secret)}")
    print(f"UPSTOX_ANALYTICS_KEY  : {C(s.upstox_analytics_key)}")
    print(f"UPSTOX_ACCESS_TOKEN   : {C(s.upstox_access_token)}")

    # ------------------------------------------------------------------ #
    # ANGEL ONE
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("ANGEL ONE LIVE TESTS")
    print("=" * 60)

    ao_results = {}

    if s.angel_one_api_key and s.angel_one_client_id and s.angel_one_totp_secret and s.angel_one_mpin:
        from src.providers.adapters.angel_one import AngelOneAdapter
        adapter = AngelOneAdapter(
            api_key=s.angel_one_api_key,
            client_id=s.angel_one_client_id,
            totp_secret=s.angel_one_totp_secret,
            mpin=s.angel_one_mpin,
        )

        # 1. Authentication
        print("\n--- 1. Authentication ---")
        t0 = time.monotonic()
        try:
            await adapter.ensure_authenticated()
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  AUTH: OK  latency={latency}ms")
            ao_results["auth"] = {"status": "PASS", "latency_ms": latency}
        except Exception as e:
            print(f"  AUTH: FAIL  error={e}")
            ao_results["auth"] = {"status": "FAIL", "error": str(e)}
            await adapter.close()
            print("  SKIPPING remaining Angel One tests — auth failed")
        else:
            # 2. Live quote — RELIANCE
            print("\n--- 2. Live Quote (RELIANCE token 2885) ---")
            t0 = time.monotonic()
            try:
                quotes = await adapter.fetch_live_quote(["2885"], exchange="NSE")
                latency = int((time.monotonic() - t0) * 1000)
                if quotes:
                    q = quotes[0]
                    print(f"  QUOTE: OK  ltp={q.get('ltp')} high={q.get('high')} low={q.get('low')} vol={q.get('tradeVolume')} latency={latency}ms")
                    ao_results["live_quote_reliance"] = {
                        "status": "PASS", "ltp": q.get("ltp"), "high": q.get("high"),
                        "low": q.get("low"), "volume": q.get("tradeVolume"), "latency_ms": latency,
                        "provider": q.get("provider"), "source_type": q.get("sourceType"),
                    }
                else:
                    print(f"  QUOTE: EMPTY  latency={latency}ms (market likely closed)")
                    ao_results["live_quote_reliance"] = {"status": "EMPTY_MARKET_CLOSED", "latency_ms": latency}
            except Exception as e:
                print(f"  QUOTE: FAIL  error={e}")
                ao_results["live_quote_reliance"] = {"status": "FAIL", "error": str(e)}

            # 3. Live quote — NIFTY index (token 99926000)
            print("\n--- 3. Live Quote (NIFTY index token 99926000) ---")
            t0 = time.monotonic()
            try:
                quotes = await adapter.fetch_live_quote(["99926000"], exchange="NSE")
                latency = int((time.monotonic() - t0) * 1000)
                if quotes:
                    q = quotes[0]
                    print(f"  NIFTY: OK  ltp={q.get('ltp')} latency={latency}ms")
                    ao_results["live_quote_nifty"] = {"status": "PASS", "ltp": q.get("ltp"), "latency_ms": latency}
                else:
                    print(f"  NIFTY: EMPTY  latency={latency}ms")
                    ao_results["live_quote_nifty"] = {"status": "EMPTY_MARKET_CLOSED", "latency_ms": latency}
            except Exception as e:
                print(f"  NIFTY: FAIL  error={e}")
                ao_results["live_quote_nifty"] = {"status": "FAIL", "error": str(e)}

            # 4. Historical OHLCV — RELIANCE 1d (last 10 trading days)
            print("\n--- 4. Historical 1d RELIANCE (token 2885) ---")
            t0 = time.monotonic()
            try:
                today = datetime.date.today()
                from_d = (today - datetime.timedelta(days=20)).strftime("%Y-%m-%d 09:15")
                to_d = today.strftime("%Y-%m-%d 15:30")
                bars = await adapter.fetch_historical_ohlcv("RELIANCE", "2885", from_d, to_d, "1d", "NSE")
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  HIST_1D: OK  bars={len(bars)} latency={latency}ms")
                if bars:
                    b = bars[-1]
                    print(f"  Last bar: time={b.get('time')} open={b.get('open')} close={b.get('close')} vol={b.get('volume')} provider={b.get('provider')}")
                ao_results["hist_1d_reliance"] = {
                    "status": "PASS", "bars": len(bars), "latency_ms": latency,
                    "last_bar": {k: bars[-1].get(k) for k in ["time", "open", "close", "volume", "provider", "sourceType"]} if bars else None
                }
            except Exception as e:
                print(f"  HIST_1D: FAIL  error={e}")
                ao_results["hist_1d_reliance"] = {"status": "FAIL", "error": str(e)}

            # 5. Historical OHLCV — NIFTY 5m (last 2 days)
            print("\n--- 5. Historical 5m NIFTY (token 99926000) ---")
            t0 = time.monotonic()
            try:
                today = datetime.date.today()
                from_d = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%d 09:15")
                to_d = today.strftime("%Y-%m-%d 15:30")
                bars = await adapter.fetch_historical_ohlcv("NIFTY", "99926000", from_d, to_d, "5m", "NSE")
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  HIST_5M_NIFTY: OK  bars={len(bars)} latency={latency}ms")
                if bars:
                    b = bars[-1]
                    print(f"  Last bar: time={b.get('time')} close={b.get('close')}")
                ao_results["hist_5m_nifty"] = {"status": "PASS", "bars": len(bars), "latency_ms": latency}
            except Exception as e:
                print(f"  HIST_5M_NIFTY: FAIL  error={e}")
                ao_results["hist_5m_nifty"] = {"status": "FAIL", "error": str(e)}

            # 6. Historical OHLCV — HDFCBANK 1m (last 1 day)
            print("\n--- 6. Historical 1m HDFCBANK (token 1333) ---")
            t0 = time.monotonic()
            try:
                today = datetime.date.today()
                from_d = (today - datetime.timedelta(days=3)).strftime("%Y-%m-%d 09:15")
                to_d = today.strftime("%Y-%m-%d 15:30")
                bars = await adapter.fetch_historical_ohlcv("HDFCBANK", "1333", from_d, to_d, "1m", "NSE")
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  HIST_1M_HDFCBANK: OK  bars={len(bars)} latency={latency}ms")
                ao_results["hist_1m_hdfcbank"] = {"status": "PASS", "bars": len(bars), "latency_ms": latency}
            except Exception as e:
                print(f"  HIST_1M_HDFCBANK: FAIL  error={e}")
                ao_results["hist_1m_hdfcbank"] = {"status": "FAIL", "error": str(e)}

            # 7. Historical OHLCV — NIFTY FUT (near-month)
            print("\n--- 7. Historical 1d NIFTY FUT ---")
            t0 = time.monotonic()
            try:
                # NIFTY Sep 2026 future token
                bars = await adapter.fetch_historical_ohlcv(
                    "NIFTY29SEP26FUT", "35004",
                    "2026-09-01 09:15", "2026-09-17 15:30",
                    "1d", "NFO"
                )
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  NIFTY_FUT_1D: OK  bars={len(bars)} latency={latency}ms")
                if bars:
                    b = bars[-1]
                    print(f"  Last bar: time={b.get('time')} close={b.get('close')}")
                ao_results["hist_1d_nifty_fut"] = {"status": "PASS", "bars": len(bars), "latency_ms": latency}
            except Exception as e:
                print(f"  NIFTY_FUT_1D: FAIL  error={e}")
                ao_results["hist_1d_nifty_fut"] = {"status": "FAIL", "error": str(e)}

            # 8. Historical OI — NIFTY FUT
            print("\n--- 8. Historical OI (getOIData) NIFTY FUT ---")
            t0 = time.monotonic()
            try:
                oi_records = await adapter.fetch_historical_oi(
                    "35004", "NFO", "1d",
                    "2026-09-01 09:15", "2026-09-17 15:30"
                )
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  HIST_OI_NIFTY_FUT: OK  records={len(oi_records)} latency={latency}ms")
                if oi_records:
                    r = oi_records[-1]
                    print(f"  Last OI record: time={r.get('time')} oi={r.get('openInterest')} provider={r.get('provider')}")
                ao_results["hist_oi_nifty_fut"] = {
                    "status": "PASS", "records": len(oi_records), "latency_ms": latency,
                    "last_oi": oi_records[-1].get("openInterest") if oi_records else None
                }
            except Exception as e:
                print(f"  HIST_OI_NIFTY_FUT: FAIL  error={e}")
                ao_results["hist_oi_nifty_fut"] = {"status": "FAIL", "error": str(e)}

            # 9. Option Greeks — NIFTY
            print("\n--- 9. Option Greeks (optionGreek) NIFTY ---")
            t0 = time.monotonic()
            try:
                greeks = await adapter.fetch_option_greeks("NIFTY", "29SEP2026")
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  GREEKS_NIFTY: OK  contracts={len(greeks)} latency={latency}ms")
                if greeks:
                    g = greeks[0]
                    print(f"  Sample: strike={g.get('strike')} type={g.get('optionType')} delta={g.get('delta')} iv={g.get('iv')} oi={g.get('oi')}")
                ao_results["greeks_nifty"] = {
                    "status": "PASS", "contracts": len(greeks), "latency_ms": latency,
                    "sample": {k: greeks[0].get(k) for k in ["strike", "optionType", "delta", "iv", "oi"]} if greeks else None
                }
            except Exception as e:
                print(f"  GREEKS_NIFTY: FAIL  error={e}")
                ao_results["greeks_nifty"] = {"status": "FAIL", "error": str(e)}

            # 10. LTP lightweight
            print("\n--- 10. getLtpData (HDFCBANK token 1333) ---")
            t0 = time.monotonic()
            try:
                ltp_data = await adapter.fetch_ltp("1333", "NSE")
                latency = int((time.monotonic() - t0) * 1000)
                print(f"  LTP: OK  ltp={ltp_data.get('ltp')} latency={latency}ms")
                ao_results["ltp_hdfcbank"] = {"status": "PASS", "ltp": ltp_data.get("ltp"), "latency_ms": latency}
            except Exception as e:
                print(f"  LTP: FAIL  error={e}")
                ao_results["ltp_hdfcbank"] = {"status": "FAIL", "error": str(e)}

            await adapter.close()

    else:
        print("  SKIPPING — credentials not fully configured")
        ao_results["auth"] = {"status": "SKIP", "reason": "credentials_missing"}

    # ------------------------------------------------------------------ #
    # UPSTOX
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("UPSTOX LIVE TESTS")
    print("=" * 60)

    up_results = {}

    if s.upstox_api_key:
        from src.providers.adapters.upstox import UpstoxAdapter
        adapter_up = UpstoxAdapter(
            api_key=s.upstox_api_key,
            api_secret=s.upstox_api_secret or "",
            redirect_uri=s.upstox_redirect_uri or "http://localhost:8200/v1/auth/upstox/callback",
        )

        # Prefer analytics key
        if s.upstox_analytics_key:
            await adapter_up.set_analytics_token(s.upstox_analytics_key)
            print("\n  Using analytics key (valid until 2027-09-03)")
        if s.upstox_access_token:
            await adapter_up.set_access_token(s.upstox_access_token)
            print("  OAuth access token also set")

        # 1. Historical V3 — RELIANCE 1d
        print("\n--- 1. Historical V3 1d RELIANCE ---")
        t0 = time.monotonic()
        try:
            today = datetime.date.today()
            from_d = (today - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
            to_d = today.strftime("%Y-%m-%d")
            bars = await adapter_up.fetch_historical_ohlcv(
                "NSE_EQ|INE002A01018", "1d", from_d, to_d
            )
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  HIST_1D_RELIANCE: OK  bars={len(bars)} latency={latency}ms")
            if bars:
                b = bars[-1]
                print(f"  Last bar: time={b.get('time')} close={b.get('close')} oi={b.get('oi')} provider={b.get('provider')}")
            up_results["hist_1d_reliance"] = {
                "status": "PASS", "bars": len(bars), "latency_ms": latency,
                "last_bar": {k: bars[-1].get(k) for k in ["time", "close", "oi", "provider", "sourceType"]} if bars else None
            }
        except Exception as e:
            print(f"  HIST_1D_RELIANCE: FAIL  error={e}")
            up_results["hist_1d_reliance"] = {"status": "FAIL", "error": str(e)}

        # 2. Historical V3 — NIFTY 1m
        print("\n--- 2. Historical V3 1m NIFTY ---")
        t0 = time.monotonic()
        try:
            today = datetime.date.today()
            from_d = (today - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
            to_d = today.strftime("%Y-%m-%d")
            bars = await adapter_up.fetch_historical_ohlcv(
                "NSE_INDEX|Nifty 50", "1m", from_d, to_d
            )
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  HIST_1M_NIFTY: OK  bars={len(bars)} latency={latency}ms")
            if bars:
                b = bars[-1]
                print(f"  Last bar: time={b.get('time')} close={b.get('close')}")
            up_results["hist_1m_nifty"] = {"status": "PASS", "bars": len(bars), "latency_ms": latency}
        except Exception as e:
            print(f"  HIST_1M_NIFTY: FAIL  error={e}")
            up_results["hist_1m_nifty"] = {"status": "FAIL", "error": str(e)}

        # 3. Historical V3 — HDFCBANK 1w
        print("\n--- 3. Historical V3 1w HDFCBANK ---")
        t0 = time.monotonic()
        try:
            today = datetime.date.today()
            from_d = (today - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
            to_d = today.strftime("%Y-%m-%d")
            bars = await adapter_up.fetch_historical_ohlcv(
                "NSE_EQ|INE040A01034", "1w", from_d, to_d
            )
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  HIST_1W_HDFCBANK: OK  bars={len(bars)} latency={latency}ms")
            up_results["hist_1w_hdfcbank"] = {"status": "PASS", "bars": len(bars), "latency_ms": latency}
        except Exception as e:
            print(f"  HIST_1W_HDFCBANK: FAIL  error={e}")
            up_results["hist_1w_hdfcbank"] = {"status": "FAIL", "error": str(e)}

        # 4. Option chain — NIFTY
        print("\n--- 4. Option Chain NIFTY ---")
        t0 = time.monotonic()
        try:
            chain = await adapter_up.fetch_option_chain("NSE_INDEX|Nifty 50", "2026-09-29")
            latency = int((time.monotonic() - t0) * 1000)
            rows = len(chain.get("data", {}).get("option_chain_data", []) or [])
            spot = chain.get("data", {}).get("last_price")
            print(f"  OPTION_CHAIN_NIFTY: OK  rows={rows} spot={spot} latency={latency}ms")
            if rows > 0:
                raw = chain.get("data", {}).get("option_chain_data", [])[0]
                ce = raw.get("call_options", {}).get("market_data", {})
                print(f"  Sample CE: strike={raw.get('strike_price')} ltp={ce.get('ltp')} oi={ce.get('oi')}")
            up_results["option_chain_nifty"] = {
                "status": "PASS", "rows": rows, "spot": spot, "latency_ms": latency
            }
        except Exception as e:
            print(f"  OPTION_CHAIN_NIFTY: FAIL  error={e}")
            up_results["option_chain_nifty"] = {"status": "FAIL", "error": str(e)}

        # 5. Option chain — BANKNIFTY
        print("\n--- 5. Option Chain BANKNIFTY ---")
        t0 = time.monotonic()
        try:
            chain = await adapter_up.fetch_option_chain("NSE_INDEX|Nifty Bank", "2026-09-29")
            latency = int((time.monotonic() - t0) * 1000)
            rows = len(chain.get("data", {}).get("option_chain_data", []) or [])
            spot = chain.get("data", {}).get("last_price")
            print(f"  OPTION_CHAIN_BANKNIFTY: OK  rows={rows} spot={spot} latency={latency}ms")
            up_results["option_chain_banknifty"] = {"status": "PASS", "rows": rows, "spot": spot, "latency_ms": latency}
        except Exception as e:
            print(f"  OPTION_CHAIN_BANKNIFTY: FAIL  error={e}")
            up_results["option_chain_banknifty"] = {"status": "FAIL", "error": str(e)}

        # 6. LTP V3
        print("\n--- 6. LTP V3 RELIANCE + HDFCBANK ---")
        t0 = time.monotonic()
        try:
            ltp = await adapter_up.fetch_ltp(["NSE_EQ|INE002A01018", "NSE_EQ|INE040A01034"])
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  LTP_V3: OK  latency={latency}ms data={ltp}")
            up_results["ltp_v3"] = {"status": "PASS", "latency_ms": latency, "data": ltp}
        except Exception as e:
            print(f"  LTP_V3: FAIL  error={e}")
            up_results["ltp_v3"] = {"status": "FAIL", "error": str(e)}

        # 7. Option Greeks V3 — NIFTY 5 contracts
        print("\n--- 7. Option Greeks V3 (5 NIFTY contracts) ---")
        t0 = time.monotonic()
        try:
            # Use representative NIFTY option keys
            nifty_keys = [
                "NSE_FO|51001", "NSE_FO|51002", "NSE_FO|51003",
                "NSE_FO|51004", "NSE_FO|51005"
            ]
            greeks = await adapter_up.fetch_option_greeks(nifty_keys)
            latency = int((time.monotonic() - t0) * 1000)
            data = greeks.get("data", {})
            count = len(data) if isinstance(data, dict) else 0
            print(f"  GREEKS_V3: OK  keys_returned={count} latency={latency}ms")
            up_results["greeks_v3"] = {"status": "PASS", "keys_returned": count, "latency_ms": latency}
        except Exception as e:
            print(f"  GREEKS_V3: FAIL (may need active option keys)  error={e}")
            up_results["greeks_v3"] = {"status": "FAIL", "error": str(e)}

        # 8. OHLC V3
        print("\n--- 8. OHLC V3 TCS ---")
        t0 = time.monotonic()
        try:
            ohlc = await adapter_up.fetch_ohlc(["NSE_EQ|INE467B01029"])
            latency = int((time.monotonic() - t0) * 1000)
            print(f"  OHLC_V3: OK  latency={latency}ms data keys={list(ohlc.get('data', {}).keys())[:3]}")
            up_results["ohlc_v3"] = {"status": "PASS", "latency_ms": latency}
        except Exception as e:
            print(f"  OHLC_V3: FAIL  error={e}")
            up_results["ohlc_v3"] = {"status": "FAIL", "error": str(e)}

        # 9. Full Quote V2
        print("\n--- 9. Full Quote V2 INFY ---")
        t0 = time.monotonic()
        try:
            fq = await adapter_up.fetch_full_quote(["NSE_EQ|INE009A01021"])
            latency = int((time.monotonic() - t0) * 1000)
            data = fq.get("data", {})
            keys = list(data.keys()) if isinstance(data, dict) else []
            print(f"  FULL_QUOTE_V2: OK  latency={latency}ms instruments_returned={len(keys)}")
            up_results["full_quote_v2"] = {"status": "PASS", "latency_ms": latency, "instruments": len(keys)}
        except Exception as e:
            print(f"  FULL_QUOTE_V2: FAIL  error={e}")
            up_results["full_quote_v2"] = {"status": "FAIL", "error": str(e)}

        await adapter_up.aclose()
    else:
        print("  SKIPPING — Upstox API key not configured")
        up_results["skip"] = "credentials_missing"

    # ------------------------------------------------------------------ #
    # SUMMARY
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print("\nANGEL ONE:")
    for k, v in ao_results.items():
        status = v.get("status", "?") if isinstance(v, dict) else str(v)
        latency = v.get("latency_ms") if isinstance(v, dict) else None
        lat_str = f" ({latency}ms)" if latency else ""
        print(f"  {k:35s}: {status}{lat_str}")

    print("\nUPSTOX:")
    for k, v in up_results.items():
        status = v.get("status", "?") if isinstance(v, dict) else str(v)
        latency = v.get("latency_ms") if isinstance(v, dict) else None
        lat_str = f" ({latency}ms)" if latency else ""
        print(f"  {k:35s}: {status}{lat_str}")

    # Compile overall verdict
    ao_pass = sum(1 for v in ao_results.values() if isinstance(v, dict) and v.get("status") == "PASS")
    ao_fail = sum(1 for v in ao_results.values() if isinstance(v, dict) and v.get("status") == "FAIL")
    up_pass = sum(1 for v in up_results.values() if isinstance(v, dict) and v.get("status") == "PASS")
    up_fail = sum(1 for v in up_results.values() if isinstance(v, dict) and v.get("status") == "FAIL")

    print(f"\nAngel One: {ao_pass} PASS, {ao_fail} FAIL")
    print(f"Upstox:    {up_pass} PASS, {up_fail} FAIL")

    return ao_results, up_results


if __name__ == "__main__":
    ao, up = asyncio.run(run())
