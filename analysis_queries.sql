-- ============================================================
-- Polymarket Lag Analysis Queries
-- Run these after 1-2 weeks of data collection
-- ============================================================


-- ── 1. HOW LONG DOES POLYMARKET TAKE TO REPRICE? ────────────
-- For every BTC move > 0.5% in 5 min, measure how long until
-- Polymarket probability shifts by at least 3%

WITH btc_moves AS (
    SELECT
        ts,
        price,
        LAG(price, 5) OVER (ORDER BY ts) AS price_5min_ago,
        (price - LAG(price, 5) OVER (ORDER BY ts))
            / LAG(price, 5) OVER (ORDER BY ts) * 100 AS pct_move
    FROM btc_price_log
),
significant_moves AS (
    SELECT ts AS move_ts, price, pct_move
    FROM btc_moves
    WHERE ABS(pct_move) > 0.5
)
SELECT
    sm.move_ts,
    sm.pct_move,
    p.market_id,
    p.question,
    -- Probability at the time of BTC move
    FIRST_VALUE(p.yes_price) OVER (
        PARTITION BY sm.move_ts, p.market_id
        ORDER BY p.ts
    ) AS prob_at_move,
    -- Probability 5 min later
    MIN(CASE WHEN p.ts >= sm.move_ts + INTERVAL '5 min' THEN p.yes_price END)
        OVER (PARTITION BY sm.move_ts, p.market_id) AS prob_5min_later,
    -- Probability 10 min later
    MIN(CASE WHEN p.ts >= sm.move_ts + INTERVAL '10 min' THEN p.yes_price END)
        OVER (PARTITION BY sm.move_ts, p.market_id) AS prob_10min_later
FROM significant_moves sm
JOIN polymarket_prob_log p
    ON p.ts BETWEEN sm.move_ts AND sm.move_ts + INTERVAL '20 min'
ORDER BY sm.move_ts DESC;


-- ── 2. AVERAGE LAG BY MOVE SIZE ─────────────────────────────
-- Does a bigger BTC move = faster Polymarket repricing?

WITH btc_moves AS (
    SELECT
        ts,
        price,
        (price - LAG(price, 5) OVER (ORDER BY ts))
            / LAG(price, 5) OVER (ORDER BY ts) * 100 AS pct_move
    FROM btc_price_log
),
poly_changes AS (
    SELECT
        market_id,
        ts,
        yes_price,
        LAG(yes_price) OVER (PARTITION BY market_id ORDER BY ts) AS prev_yes,
        ABS(yes_price - LAG(yes_price) OVER (PARTITION BY market_id ORDER BY ts)) AS prob_change
    FROM polymarket_prob_log
)
SELECT
    CASE
        WHEN ABS(bm.pct_move) BETWEEN 0.5 AND 1.0 THEN '0.5-1.0%'
        WHEN ABS(bm.pct_move) BETWEEN 1.0 AND 2.0 THEN '1.0-2.0%'
        WHEN ABS(bm.pct_move) > 2.0              THEN '2.0%+'
    END AS move_bucket,
    COUNT(*)                            AS n_events,
    AVG(EXTRACT(EPOCH FROM (pc.ts - bm.ts)) / 60) AS avg_lag_minutes,
    MIN(EXTRACT(EPOCH FROM (pc.ts - bm.ts)) / 60) AS min_lag_minutes,
    AVG(pc.prob_change)                 AS avg_prob_shift
FROM btc_moves bm
JOIN poly_changes pc
    ON pc.ts BETWEEN bm.ts AND bm.ts + INTERVAL '15 min'
    AND pc.prob_change > 0.03
WHERE ABS(bm.pct_move) > 0.5
GROUP BY move_bucket
ORDER BY move_bucket;


-- ── 3. FUNDING RATE VS POLYMARKET PROBABILITY ───────────────
-- Is there correlation between extreme funding and mispriced markets?

SELECT
    DATE_TRUNC('hour', f.ts)            AS hour,
    AVG(f.funding_rate)                 AS avg_funding_rate,
    AVG(p.yes_price)                    AS avg_btc_market_yes_price,
    COUNT(DISTINCT p.market_id)         AS active_markets,
    CORR(f.funding_rate, p.yes_price)  AS correlation
FROM funding_rate_log f
JOIN polymarket_prob_log p
    ON DATE_TRUNC('hour', p.ts) = DATE_TRUNC('hour', f.ts)
GROUP BY DATE_TRUNC('hour', f.ts)
ORDER BY hour DESC
LIMIT 168;  -- last week


-- ── 4. PEAK MISPRICING WINDOW ───────────────────────────────
-- After a BTC move, at what minute is Polymarket most stale?
-- This tells you the optimal entry window for the arb bot

WITH btc_moves AS (
    SELECT ts AS move_ts, price,
        (price - LAG(price, 5) OVER (ORDER BY ts))
            / LAG(price, 5) OVER (ORDER BY ts) * 100 AS pct_move
    FROM btc_price_log
),
significant_moves AS (
    SELECT move_ts, price, pct_move
    FROM btc_moves WHERE ABS(pct_move) > 0.5
),
poly_at_move AS (
    SELECT
        sm.move_ts,
        p.market_id,
        MIN(p.yes_price) AS prob_at_move
    FROM significant_moves sm
    JOIN polymarket_prob_log p ON p.ts BETWEEN sm.move_ts - INTERVAL '2 min' AND sm.move_ts
    GROUP BY sm.move_ts, p.market_id
)
SELECT
    FLOOR(EXTRACT(EPOCH FROM (p.ts - sm.move_ts)) / 60) AS minutes_after_move,
    AVG(ABS(p.yes_price - pam.prob_at_move))            AS avg_prob_drift,
    COUNT(*)                                             AS n_observations
FROM significant_moves sm
JOIN polymarket_prob_log p ON p.ts BETWEEN sm.move_ts AND sm.move_ts + INTERVAL '20 min'
JOIN poly_at_move pam ON pam.move_ts = sm.move_ts AND pam.market_id = p.market_id
GROUP BY minutes_after_move
ORDER BY minutes_after_move;


-- ── 5. QUICK HEALTH CHECK ───────────────────────────────────
-- Run this first to confirm data is flowing correctly

SELECT
    'btc_price_log'         AS table_name,
    COUNT(*)                AS total_rows,
    MIN(ts)                 AS first_record,
    MAX(ts)                 AS latest_record,
    ROUND(COUNT(*) / GREATEST(EXTRACT(EPOCH FROM (MAX(ts) - MIN(ts))) / 3600, 1), 1) AS rows_per_hour
FROM btc_price_log
UNION ALL
SELECT
    'funding_rate_log',
    COUNT(*), MIN(ts), MAX(ts),
    ROUND(COUNT(*) / GREATEST(EXTRACT(EPOCH FROM (MAX(ts) - MIN(ts))) / 3600, 1), 1)
FROM funding_rate_log
UNION ALL
SELECT
    'polymarket_prob_log',
    COUNT(*), MIN(ts), MAX(ts),
    ROUND(COUNT(*) / GREATEST(EXTRACT(EPOCH FROM (MAX(ts) - MIN(ts))) / 3600, 1), 1)
FROM polymarket_prob_log;
