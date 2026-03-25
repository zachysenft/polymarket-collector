import os
import logging
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_tick_log (
    id       BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL,
    product  TEXT NOT NULL,
    price    NUMERIC(14,4) NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlcv (
    id          BIGSERIAL PRIMARY KEY,
    product     TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    granularity TEXT NOT NULL,
    open        NUMERIC(14,4) NOT NULL,
    high        NUMERIC(14,4) NOT NULL,
    low         NUMERIC(14,4) NOT NULL,
    close       NUMERIC(14,4) NOT NULL,
    volume      NUMERIC(20,4) NOT NULL,
    UNIQUE (product, ts, granularity)
);

CREATE TABLE IF NOT EXISTS indicators (
    id          BIGSERIAL PRIMARY KEY,
    product     TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    granularity TEXT NOT NULL,
    rsi_14      NUMERIC(8,4),
    macd        NUMERIC(14,6),
    macd_signal NUMERIC(14,6),
    macd_hist   NUMERIC(14,6),
    bb_upper    NUMERIC(14,4),
    bb_middle   NUMERIC(14,4),
    bb_lower    NUMERIC(14,4),
    UNIQUE (product, ts, granularity)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id           BIGSERIAL PRIMARY KEY,
    run_ts       TIMESTAMPTZ NOT NULL,
    product      TEXT NOT NULL,
    granularity  TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    trades       INTEGER,
    win_rate     NUMERIC(5,2),
    avg_return   NUMERIC(8,4),
    total_return NUMERIC(8,4),
    max_drawdown NUMERIC(8,4)
);

CREATE INDEX IF NOT EXISTS idx_tick_product_ts ON price_tick_log(product, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup ON ohlcv(product, granularity, ts DESC);
CREATE INDEX IF NOT EXISTS idx_indicators_lookup ON indicators(product, granularity, ts DESC);
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


def insert_price_tick(ts, product, price):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO price_tick_log (ts, product, price) VALUES (%s, %s, %s)",
        (ts, product, price)
    )
    conn.commit()
    cur.close()
    conn.close()


def upsert_ohlcv(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO ohlcv (product, ts, granularity, open, high, low, close, volume)
        VALUES %s
        ON CONFLICT (product, ts, granularity)
        DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                      close=EXCLUDED.close, volume=EXCLUDED.volume
    """, [(
        r["product"], r["ts"], r["granularity"],
        r["open"], r["high"], r["low"], r["close"], r["volume"]
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def upsert_indicators(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO indicators
            (product, ts, granularity, rsi_14, macd, macd_signal, macd_hist,
             bb_upper, bb_middle, bb_lower)
        VALUES %s
        ON CONFLICT (product, ts, granularity)
        DO UPDATE SET rsi_14=EXCLUDED.rsi_14, macd=EXCLUDED.macd,
                      macd_signal=EXCLUDED.macd_signal, macd_hist=EXCLUDED.macd_hist,
                      bb_upper=EXCLUDED.bb_upper, bb_middle=EXCLUDED.bb_middle,
                      bb_lower=EXCLUDED.bb_lower
    """, [(
        r["product"], r["ts"], r["granularity"],
        r.get("rsi_14"), r.get("macd"), r.get("macd_signal"), r.get("macd_hist"),
        r.get("bb_upper"), r.get("bb_middle"), r.get("bb_lower")
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def insert_backtest_results(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO backtest_results
            (run_ts, product, granularity, strategy, trades, win_rate,
             avg_return, total_return, max_drawdown)
        VALUES %s
    """, [(
        r["run_ts"], r["product"], r["granularity"], r["strategy"],
        r["trades"], r["win_rate"], r["avg_return"], r["total_return"],
        r["max_drawdown"]
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def get_ohlcv_df(product, granularity, limit=200):
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT ts, open, high, low, close, volume
        FROM ohlcv
        WHERE product = %s AND granularity = %s
        ORDER BY ts DESC
        LIMIT %s
    """, conn, params=(product, granularity, limit))
    conn.close()
    if df.empty:
        return df
    df = df.sort_values("ts").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def get_latest_ohlcv_ts(product, granularity):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(ts) FROM ohlcv WHERE product = %s AND granularity = %s",
        (product, granularity)
    )
    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


def get_full_dataset(product, granularity):
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT o.ts, o.open, o.high, o.low, o.close, o.volume,
               i.rsi_14, i.macd, i.macd_signal, i.macd_hist,
               i.bb_upper, i.bb_middle, i.bb_lower
        FROM ohlcv o
        LEFT JOIN indicators i
            ON o.product = i.product AND o.ts = i.ts AND o.granularity = i.granularity
        WHERE o.product = %s AND o.granularity = %s
        ORDER BY o.ts
    """, conn, params=(product, granularity))
    conn.close()
    if df.empty:
        return df
    for col in ["open", "high", "low", "close", "volume",
                "rsi_14", "macd", "macd_signal", "macd_hist",
                "bb_upper", "bb_middle", "bb_lower"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
