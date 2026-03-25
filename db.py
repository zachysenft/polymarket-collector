import os
import psycopg2
from psycopg2.extras import execute_values
import logging

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS btc_price_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    price           NUMERIC(12,2) NOT NULL,
    volume_24h      NUMERIC(20,2),
    source          TEXT DEFAULT 'binance'
);

CREATE TABLE IF NOT EXISTS funding_rate_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    funding_rate    NUMERIC(10,8) NOT NULL,
    next_funding_ts TIMESTAMPTZ,
    source          TEXT DEFAULT 'binance'
);

CREATE TABLE IF NOT EXISTS polymarket_prob_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    yes_price       NUMERIC(6,4) NOT NULL,
    no_price        NUMERIC(6,4) NOT NULL,
    volume          NUMERIC(20,2),
    end_date        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS kalshi_prob_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    market_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    yes_price       NUMERIC(6,4) NOT NULL,
    no_price        NUMERIC(6,4) NOT NULL,
    volume          NUMERIC(20,2),
    end_date        TIMESTAMPTZ
);

-- Index for time-series queries
CREATE INDEX IF NOT EXISTS idx_btc_price_ts       ON btc_price_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_funding_ts          ON funding_rate_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_poly_ts             ON polymarket_prob_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_poly_market_ts      ON polymarket_prob_log(market_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_kalshi_ts           ON kalshi_prob_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_kalshi_market_ts    ON kalshi_prob_log(market_id, ts DESC);

-- View for lag analysis: joins BTC moves with Polymarket response
CREATE OR REPLACE VIEW lag_analysis AS
SELECT
    b.ts                                        AS btc_ts,
    b.price                                     AS btc_price,
    p.ts                                        AS poly_ts,
    p.market_id,
    p.question,
    p.yes_price,
    EXTRACT(EPOCH FROM (p.ts - b.ts)) / 60.0   AS lag_minutes,
    LAG(b.price) OVER (ORDER BY b.ts)           AS prev_btc_price,
    (b.price - LAG(b.price) OVER (ORDER BY b.ts))
        / LAG(b.price) OVER (ORDER BY b.ts) * 100 AS btc_pct_change
FROM btc_price_log b
JOIN polymarket_prob_log p
    ON p.ts BETWEEN b.ts AND b.ts + INTERVAL '20 minutes';
"""


def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def init_schema():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(SCHEMA)
    conn.commit()
    cur.close()
    conn.close()
    log.info("Schema initialized")


def insert_btc_price(ts, price, volume_24h=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO btc_price_log (ts, price, volume_24h) VALUES (%s, %s, %s)",
        (ts, price, volume_24h)
    )
    conn.commit()
    cur.close()
    conn.close()


def insert_funding_rate(ts, funding_rate, next_funding_ts=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO funding_rate_log (ts, funding_rate, next_funding_ts) VALUES (%s, %s, %s)",
        (ts, funding_rate, next_funding_ts)
    )
    conn.commit()
    cur.close()
    conn.close()


def insert_kalshi_snapshots(rows):
    """
    rows: list of dicts with keys:
      ts, market_id, question, yes_price, no_price, volume, end_date
    """
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO kalshi_prob_log
            (ts, market_id, question, yes_price, no_price, volume, end_date)
        VALUES %s
    """, [(
        r["ts"], r["market_id"], r["question"],
        r["yes_price"], r["no_price"], r.get("volume"), r.get("end_date")
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def insert_poly_snapshots(rows):
    """
    rows: list of dicts with keys:
      ts, market_id, question, yes_price, no_price, volume, end_date
    """
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO polymarket_prob_log
            (ts, market_id, question, yes_price, no_price, volume, end_date)
        VALUES %s
    """, [(
        r["ts"], r["market_id"], r["question"],
        r["yes_price"], r["no_price"], r.get("volume"), r.get("end_date")
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()
