# F&O INSTRUMENT MASTER READINESS REPORT
**Generated:** 2026-09-15 (updated with live results)  
**Status:** ✅ POPULATED FROM ANGEL ONE SCRIP MASTER

---

## 1. CURRENT STATE

| Component | Status | Count |
|---|---|---|
| `instrument_master` table | ✅ Populated | **34,460 rows** |
| EQ instruments (Nifty-50) | ✅ Populated | 47 |
| IDX instruments (NIFTY, BANKNIFTY) | ✅ Populated | 2 |
| CRYPTO instruments (BTCUSDT) | ✅ Populated | 1 |
| FUT instruments (FUTSTK + FUTIDX) | ✅ **POPULATED** | **665** (FUTSTK: 647, FUTIDX: 18) |
| OPT instruments (OPTSTK + OPTIDX) | ✅ **POPULATED** | **33,745** (OPTSTK: 28,455, OPTIDX: 5,290) |
| `instrument_provider_mapping` | ✅ Populated | **68,915 rows** |

**Population script:** `scripts/load_fno_instrument_master.py`  
**Source:** Angel One OpenAPI scrip master (`margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json`)  
**Total instruments downloaded:** 147,745 (all exchanges)  
**NFO instruments loaded:** 34,410 F&O contracts  

---

## 2. HOW IT WAS POPULATED

```bash
APP_ENV=local python3 scripts/load_fno_instrument_master.py
```

The Angel One OpenAPI scrip master is publicly accessible (no authentication required) and contains every listed F&O contract with:
- `token` → Angel One numeric instrument token
- `symbol` → NSE trading symbol (e.g. `RELIANCE29SEP26FUT`, `NIFTY29SEP2625000CE`)
- `name` → underlying name (e.g. `RELIANCE`, `NIFTY`)
- `expiry` → expiry date string (e.g. `29SEP2026`)
- `strike` → raw strike × 100 (e.g. 2500000 = 25000.00)
- `instrumenttype` → FUTSTK | FUTIDX | OPTSTK | OPTIDX
- `lotsize`, `tick_size`

**Strike semantics:** `strike_raw / 100 = actual_price`  
**active_from:** Set to `2020-01-01` (conservative — covers the backfill window)  
**active_to:** Set to expiry date  

---

## 3. SCHEMA CAPABILITY FOR F&O

The `instrument_master` schema fully supports F&O contract identity:

```sql
-- Futures example
instrument_id    = 'NFO:RELIANCE29SEP26FUT'
trading_symbol   = 'RELIANCE29SEP26FUT'
exchange         = 'NFO'
instrument_type  = 'FUTSTK'
instrument_class = 'FUT'
underlying       = 'RELIANCE'
expiry           = '2026-09-29'
lot_size         = 500
tick_size        = 10.0
active_from      = '2020-01-01'
active_to        = '2026-09-29'
angel_token      = '68777'

-- Options example
instrument_id    = 'NFO:RELIANCE29SEP261210PE'
instrument_type  = 'OPTSTK'
instrument_class = 'OPT'
strike           = 1210.00
option_type      = 'PE'
expiry           = '2026-09-29'
```

---

## 4. INSTRUMENT_PROVIDER_MAPPING

```sql
SELECT provider, COUNT(*) FROM instrument_provider_mapping GROUP BY provider;
-- angel_one: 68,869 rows
-- upstox:    46 rows (EQ only — F&O Upstox keys not in scrip master)
-- Total:     68,915 rows
```

Each F&O contract has an Angel One token mapped in `instrument_provider_mapping` with:
- `provider = 'angel_one'`
- `provider_instrument_id` = numeric token (e.g. `68777` for RELIANCE29SEP26FUT)
- `exchange_segment = 'NFO_FO'`
- `valid_from = '2020-01-01'`

---

## 5. DB-BASED TOKEN LOOKUP IN HISTORICAL ENGINE

`src/engines/historical_engine.py` now resolves Angel One tokens from `instrument_master` at runtime:

```python
# Priority 1: Look up from instrument_master (DB)
_row = await conn.execute(
    "SELECT angel_token FROM instrument_master WHERE instrument_id=:iid OR (trading_symbol=:sym AND exchange=:exch)"
)
# Priority 2: Fall back to _ANGEL_ONE_KNOWN_TOKENS dict (equities)
```

This enables F&O backfill without hardcoded token maps.

---

## 6. SERVER STARTUP

`InstrumentMasterService` loads at server startup:

```
2026-09-15T04:07:15.116042Z [info] instrument_master_loaded instrument_count=34459
```

All 34,459 instruments are in-memory at startup for fast lookups.

---

## 7. VERDICT

```
F&O INSTRUMENT MASTER: ✅ READY
  - 34,460 rows (EQ=47, IDX=2, FUT=665, OPT=33,745, CRYPTO=1)
  - 68,915 provider mappings (Angel One tokens for all F&O contracts)
  - DB-based token resolution active in historical_engine.py
  - InstrumentMasterService loads 34,459 instruments at startup
  - F&O backfill can resolve instrument tokens without hardcoded maps

STATUS: FULLY READY ✅
```
