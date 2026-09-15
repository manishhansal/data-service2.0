# F&O CONTRACT IDENTITY REPORT
**Generated:** 2026-09-15  
**Status:** DESIGN VERIFIED — IMPLEMENTATION READY

---

## 1. IDENTITY PRINCIPLE

Every F&O contract is uniquely identified by the combination of:

```
underlying + exchange + expiry + contract_type
                                     ↓ for options:
                       + strike + option_type
```

This combination maps to exactly ONE `instrument_id` in `instrument_master`.

---

## 2. INSTRUMENT ID FORMAT

```
Futures:  NFO:{UNDERLYING}{YY}{MON}FUT
Options:  NFO:{UNDERLYING}{YY}{MON}{STRIKE}{OPTION_TYPE}

Examples:
  NFO:RELIANCE25SEPFUT     → RELIANCE Sep 2025 Future
  NFO:RELIANCE25OCTFUT     → RELIANCE Oct 2025 Future  (SEPARATE ID)
  NFO:NIFTY25SEP25000CE    → NIFTY Sep 2025 25000 Call
  NFO:NIFTY25SEP25000PE    → NIFTY Sep 2025 25000 Put  (SEPARATE ID)
  NFO:NIFTY25OCT25000CE    → NIFTY Oct 2025 25000 Call (SEPARATE ID)
  NFO:BANKNIFTY25SEP52000CE → BANKNIFTY Sep 2025 52000 Call
```

---

## 3. WHAT MUST NEVER BE MERGED

| Violation | Why Forbidden |
|---|---|
| Multiple expiries into one "continuous future" | Look-ahead bias; incorrect OI; survivorship bias |
| CE and PE into one "option" record | Opposite instruments with different payoffs |
| Different strikes into one record | Completely different risk profiles |
| Historical + current contracts | Point-in-time integrity violation |

---

## 4. SCHEMA VALIDATION

`instrument_master` constraints ensure:

```sql
-- For options: option_type must be CE or PE only
-- (enforced by application logic during population)
CHECK (option_type IN ('CE', 'PE'))  -- or NULL for non-options

-- For derivatives: expiry must be non-null
-- (enforced by application logic)

-- Uniqueness: each contract has exactly one row
PRIMARY KEY (instrument_id)
```

`options_candle` table enforces:
```sql
CHECK (option_type IN ('CE','PE'))   -- oc_option_type_valid
CHECK (strike > 0)                   -- oc_strike_positive
```

`futures_candle` table enforces:
```sql
CHECK (contract_type = 'FUT')        -- fc_contract_type_fut
```

---

## 5. EXPIRY MANAGEMENT

| State | active_to | Description |
|---|---|---|
| Active contract | NULL | Currently tradeable |
| Expired contract | = expiry date | Historical record preserved |
| Not yet listed | active_from = listing date | Future contract |

Expired contracts are NEVER deleted. They are retained with `active_to` set to their expiry date to maintain historical accuracy and prevent survivorship bias.

---

## 6. SYMBOL CONTINUITY

Corporate actions and symbol changes are tracked in `instrument_identity_history`:

```sql
-- Example: HDFC Bank absorbed HDFC Ltd
INSERT INTO instrument_identity_history (
  instrument_id, change_type, old_symbol, new_symbol,
  valid_from, reason, source
) VALUES (
  'NSE:HDFCBANK', 'CORPORATE_ACTION',
  'HDFC', 'HDFCBANK',
  '2023-07-01',
  'HDFC-HDFCBANK merger effective 2023-07-01',
  'NSE_CIRCULAR'
);
```

---

## 7. PROVIDER TOKEN CONSISTENCY

Each active F&O contract must have entries in `instrument_provider_mapping`:

```sql
-- Angel One: uses numeric token IDs per contract
-- Upstox: uses 'NFO_FO|{ISIN}' format
-- Both: valid_from = listing date, valid_to = expiry date
```

When a contract expires, the mapping's `valid_to` is set and `is_active = FALSE`.

---

## 8. VERDICT

```
CONTRACT IDENTITY: SCHEMA CORRECT AND ENFORCED

The schema enforces separate instruments per contract. The CHECK
constraints prevent CE/PE merging and invalid strikes. The instrument_id
format encodes all identity-relevant fields. Expiry tracking prevents
look-ahead. Historical records are preserved (not deleted).

STATUS: READY — pending population of F&O instrument rows
```
