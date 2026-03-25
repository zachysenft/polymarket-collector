-- ============================================================
-- Crypto Strategy Analysis Queries
-- Run in Supabase SQL editor
-- ============================================================


-- ── 1. HEALTH CHECK ──────────────────────────────────────────
SELECT
    'ohlcv'           AS table_name,
    COUNT(*)          AS total_rows,
    MIN(ts)           AS first_record,
    MAX(ts)           AS latest_record
FROM ohlcv
UNION ALL
SELECT
    'indicators', COUNT(*), MIN(ts), MAX(ts)
FROM indicators
UNION ALL
SELECT
    'price_tick_log', COUNT(*), MIN(ts), MAX(ts)
FROM price_tick_log
UNION ALL
SELECT
    'backtest_results', COUNT(*), MIN(run_ts), MAX(run_ts)
FROM backtest_results;


-- ── 2. OHLCV COVERAGE ───────────────────────────────────────
SELECT product, granularity, COUNT(*) AS candles,
       MIN(ts) AS from_ts, MAX(ts) AS to_ts
FROM ohlcv
GROUP BY product, granularity
ORDER BY product, granularity;


-- ── 3. BACKTEST LEADERBOARD ──────────────────────────────────
-- Best strategies by total return
SELECT product, granularity, strategy,
       trades, win_rate, avg_return, total_return, max_drawdown
FROM backtest_results
WHERE trades > 0
ORDER BY total_return DESC
LIMIT 20;


-- ── 4. RSI EXTREMES — What happened after oversold/overbought? ──
WITH rsi_events AS (
    SELECT i.product, i.ts, i.granularity, i.rsi_14, o.close,
           LEAD(o.close, 6) OVER (PARTITION BY i.product, i.granularity ORDER BY i.ts) AS close_6_later,
           LEAD(o.close, 12) OVER (PARTITION BY i.product, i.granularity ORDER BY i.ts) AS close_12_later
    FROM indicators i
    JOIN ohlcv o ON i.product = o.product AND i.ts = o.ts AND i.granularity = o.granularity
    WHERE i.rsi_14 < 30 OR i.rsi_14 > 70
)
SELECT product, granularity,
    CASE WHEN rsi_14 < 30 THEN 'oversold' ELSE 'overbought' END AS signal,
    COUNT(*) AS occurrences,
    ROUND(AVG((close_6_later - close) / close * 100), 3) AS avg_return_6p,
    ROUND(AVG((close_12_later - close) / close * 100), 3) AS avg_return_12p
FROM rsi_events
WHERE close_6_later IS NOT NULL
GROUP BY product, granularity, CASE WHEN rsi_14 < 30 THEN 'oversold' ELSE 'overbought' END
ORDER BY product, granularity;


-- ── 5. MACD CROSSOVERS ──────────────────────────────────────
WITH macd_cross AS (
    SELECT i.product, i.ts, i.granularity, i.macd_hist, o.close,
           LAG(i.macd_hist) OVER (PARTITION BY i.product, i.granularity ORDER BY i.ts) AS prev_hist,
           LEAD(o.close, 6) OVER (PARTITION BY i.product, i.granularity ORDER BY i.ts) AS close_6_later
    FROM indicators i
    JOIN ohlcv o ON i.product = o.product AND i.ts = o.ts AND i.granularity = o.granularity
)
SELECT product, granularity,
    CASE WHEN prev_hist <= 0 AND macd_hist > 0 THEN 'bullish' ELSE 'bearish' END AS crossover,
    COUNT(*) AS occurrences,
    ROUND(AVG((close_6_later - close) / close * 100), 3) AS avg_return_6p
FROM macd_cross
WHERE (prev_hist <= 0 AND macd_hist > 0) OR (prev_hist >= 0 AND macd_hist < 0)
  AND close_6_later IS NOT NULL
GROUP BY product, granularity, CASE WHEN prev_hist <= 0 AND macd_hist > 0 THEN 'bullish' ELSE 'bearish' END
ORDER BY product, granularity;


-- ── 6. CROSS-ASSET CORRELATION (hourly) ─────────────────────
-- Compare BTC hourly returns vs other assets
WITH hourly_returns AS (
    SELECT product, ts,
           (close - LAG(close) OVER (PARTITION BY product ORDER BY ts))
             / LAG(close) OVER (PARTITION BY product ORDER BY ts) * 100 AS pct_return
    FROM ohlcv
    WHERE granularity = '1hour'
)
SELECT
    a.product AS asset,
    ROUND(CORR(b.pct_return, a.pct_return)::numeric, 4) AS corr_with_btc,
    COUNT(*) AS data_points
FROM hourly_returns a
JOIN hourly_returns b ON a.ts = b.ts AND b.product = 'BTC-USD'
WHERE a.product != 'BTC-USD'
  AND a.pct_return IS NOT NULL AND b.pct_return IS NOT NULL
GROUP BY a.product
ORDER BY corr_with_btc DESC;
