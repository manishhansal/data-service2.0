-- Point-in-time validation SQL
SELECT 'equity_candle' AS tbl,
  COUNT(*) AS total,
  SUM(CASE WHEN source_timestamp IS NOT NULL AND received_at < source_timestamp THEN 1 ELSE 0 END) AS neg_latency,
  SUM(CASE WHEN source_timestamp > NOW() + INTERVAL '1 hour' THEN 1 ELSE 0 END) AS future_source_ts,
  SUM(CASE WHEN received_at < '2020-01-01' THEN 1 ELSE 0 END) AS impossible_received_at,
  SUM(CASE WHEN received_at IS NULL THEN 1 ELSE 0 END) AS null_received_at
FROM equity_candle
UNION ALL
SELECT 'futures_candle',
  COUNT(*),
  SUM(CASE WHEN source_timestamp IS NOT NULL AND received_at < source_timestamp THEN 1 ELSE 0 END),
  SUM(CASE WHEN source_timestamp > NOW() + INTERVAL '1 hour' THEN 1 ELSE 0 END),
  SUM(CASE WHEN received_at < '2020-01-01' THEN 1 ELSE 0 END),
  SUM(CASE WHEN received_at IS NULL THEN 1 ELSE 0 END)
FROM futures_candle
UNION ALL
SELECT 'options_candle',
  COUNT(*),
  SUM(CASE WHEN source_timestamp IS NOT NULL AND received_at < source_timestamp THEN 1 ELSE 0 END),
  SUM(CASE WHEN source_timestamp > NOW() + INTERVAL '1 hour' THEN 1 ELSE 0 END),
  SUM(CASE WHEN received_at < '2020-01-01' THEN 1 ELSE 0 END),
  SUM(CASE WHEN received_at IS NULL THEN 1 ELSE 0 END)
FROM options_candle;

-- F&O specific: expiry must not be null for futures_candle
SELECT 'futures_expiry_null' AS check_name, COUNT(*) AS violations
FROM futures_candle WHERE expiry IS NULL;

-- Futures: candle time must not be after expiry
SELECT 'futures_candle_after_expiry' AS check_name, COUNT(*) AS violations
FROM futures_candle WHERE time > expiry + INTERVAL '1 day';

-- equity_candle: no 3m intervals
SELECT 'equity_3m_candles' AS check_name, COUNT(*) AS violations
FROM equity_candle WHERE interval_str = '3m';

-- futures_candle: no 3m intervals
SELECT 'futures_3m_candles' AS check_name, COUNT(*) AS violations
FROM futures_candle WHERE interval_str = '3m';

-- OI: futures must have NULL (not 0) for unavailable OI
SELECT 'futures_oi_zero_corruption' AS check_name, COUNT(*) AS violations
FROM futures_candle WHERE open_interest = 0;

-- Duplicate check: equity
SELECT 'equity_duplicates' AS check_name, COUNT(*) AS violations
FROM (
  SELECT instrument_id, exchange, interval_str, time, COUNT(*) c
  FROM equity_candle GROUP BY instrument_id, exchange, interval_str, time
  HAVING COUNT(*) > 1
) t;

-- Duplicate check: futures
SELECT 'futures_duplicates' AS check_name, COUNT(*) AS violations
FROM (
  SELECT instrument_id, exchange, interval_str, time, COUNT(*) c
  FROM futures_candle GROUP BY instrument_id, exchange, interval_str, time
  HAVING COUNT(*) > 1
) t;

-- OHLC violations: equity
SELECT 'equity_ohlc_high_lt_low' AS check_name, COUNT(*) AS violations
FROM equity_candle WHERE high < low;

-- OHLC violations: futures
SELECT 'futures_ohlc_high_lt_low' AS check_name, COUNT(*) AS violations
FROM futures_candle WHERE high < low;
