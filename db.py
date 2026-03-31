import os
import logging
import warnings
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone

warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

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
    ema_50      NUMERIC(14,4),
    ema_200     NUMERIC(14,4),
    atr_14      NUMERIC(14,6),
    adx_14      NUMERIC(8,4),
    obv         NUMERIC(20,2),
    stoch_rsi   NUMERIC(8,4),
    vwap        NUMERIC(14,4),
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

-- Add new indicator columns (safe to re-run, errors ignored in init_schema)
DO $$ BEGIN
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS ema_50 NUMERIC(14,4);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS ema_200 NUMERIC(14,4);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS atr_14 NUMERIC(14,6);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS adx_14 NUMERIC(8,4);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS obv NUMERIC(20,2);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS stoch_rsi NUMERIC(8,4);
    ALTER TABLE indicators ADD COLUMN IF NOT EXISTS vwap NUMERIC(14,4);
    ALTER TABLE shadow_balance ADD COLUMN IF NOT EXISTS strategy TEXT;
    ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS entry_vix NUMERIC(8,4);
END $$;

CREATE TABLE IF NOT EXISTS shadow_trades (
    id              BIGSERIAL PRIMARY KEY,
    strategy        TEXT NOT NULL,
    product         TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,
    entry_ts        TIMESTAMPTZ NOT NULL,
    entry_price     NUMERIC(14,4) NOT NULL,
    exit_ts         TIMESTAMPTZ,
    exit_price      NUMERIC(14,4),
    position_size   NUMERIC(10,2) NOT NULL,
    peak_price      NUMERIC(14,4),
    sl_pct          NUMERIC(8,4),
    tp_pct          NUMERIC(8,4),
    trail_pct       NUMERIC(8,4),
    exit_reason     TEXT,
    pnl_dollars     NUMERIC(10,4),
    pnl_pct         NUMERIC(8,4),
    entry_rsi       NUMERIC(8,4),
    entry_macd_hist NUMERIC(14,6),
    entry_adx       NUMERIC(8,4),
    entry_atr       NUMERIC(14,6),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status);
CREATE INDEX IF NOT EXISTS idx_shadow_strategy ON shadow_trades(strategy, status);

CREATE TABLE IF NOT EXISTS shadow_balance (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL,
    balance NUMERIC(10,2) NOT NULL,
    event   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deribit_options (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL,
    currency         TEXT NOT NULL,
    instrument_name  TEXT NOT NULL,
    expiry_ts        TIMESTAMPTZ NOT NULL,
    strike           NUMERIC(14,2) NOT NULL,
    option_type      TEXT NOT NULL,
    mark_iv          NUMERIC(8,4),
    bid_iv           NUMERIC(8,4),
    ask_iv           NUMERIC(8,4),
    delta            NUMERIC(10,6),
    gamma            NUMERIC(14,10),
    vega             NUMERIC(14,6),
    theta            NUMERIC(14,6),
    open_interest    NUMERIC(20,2),
    volume           NUMERIC(20,2),
    mark_price       NUMERIC(14,6),
    underlying_price NUMERIC(14,2),
    UNIQUE (instrument_name, ts)
);

CREATE TABLE IF NOT EXISTS deribit_surface (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    currency     TEXT NOT NULL,
    expiry_ts    TIMESTAMPTZ NOT NULL,
    days_to_exp  NUMERIC(8,2),
    atm_iv       NUMERIC(8,4),
    skew_25d     NUMERIC(8,4),
    pc_oi_ratio  NUMERIC(8,4),
    total_oi     NUMERIC(20,2),
    total_volume NUMERIC(20,2),
    UNIQUE (currency, expiry_ts, ts)
);

CREATE TABLE IF NOT EXISTS macro_daily (
    id      BIGSERIAL PRIMARY KEY,
    ts      DATE NOT NULL,
    symbol  TEXT NOT NULL,
    open    NUMERIC(14,4),
    high    NUMERIC(14,4),
    low     NUMERIC(14,4),
    close   NUMERIC(14,4) NOT NULL,
    volume  NUMERIC(20,0),
    UNIQUE (symbol, ts)
);

CREATE TABLE IF NOT EXISTS shadow_checkin_snapshot (
    id       BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL,
    strategy TEXT NOT NULL,
    balance  NUMERIC(10,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkin_snapshot_ts ON shadow_checkin_snapshot(ts DESC);

CREATE INDEX IF NOT EXISTS idx_deribit_options_lookup ON deribit_options(currency, ts DESC);
CREATE INDEX IF NOT EXISTS idx_deribit_options_expiry ON deribit_options(currency, expiry_ts, ts DESC);
CREATE INDEX IF NOT EXISTS idx_deribit_surface_lookup ON deribit_surface(currency, ts DESC);
CREATE INDEX IF NOT EXISTS idx_macro_daily_lookup ON macro_daily(symbol, ts DESC);
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
             bb_upper, bb_middle, bb_lower,
             ema_50, ema_200, atr_14, adx_14, obv, stoch_rsi, vwap)
        VALUES %s
        ON CONFLICT (product, ts, granularity)
        DO UPDATE SET rsi_14=EXCLUDED.rsi_14, macd=EXCLUDED.macd,
                      macd_signal=EXCLUDED.macd_signal, macd_hist=EXCLUDED.macd_hist,
                      bb_upper=EXCLUDED.bb_upper, bb_middle=EXCLUDED.bb_middle,
                      bb_lower=EXCLUDED.bb_lower,
                      ema_50=EXCLUDED.ema_50, ema_200=EXCLUDED.ema_200,
                      atr_14=EXCLUDED.atr_14, adx_14=EXCLUDED.adx_14,
                      obv=EXCLUDED.obv, stoch_rsi=EXCLUDED.stoch_rsi,
                      vwap=EXCLUDED.vwap
    """, [(
        r["product"], r["ts"], r["granularity"],
        r.get("rsi_14"), r.get("macd"), r.get("macd_signal"), r.get("macd_hist"),
        r.get("bb_upper"), r.get("bb_middle"), r.get("bb_lower"),
        r.get("ema_50"), r.get("ema_200"), r.get("atr_14"), r.get("adx_14"),
        r.get("obv"), r.get("stoch_rsi"), r.get("vwap")
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


def get_latest_prices(products):
    """Get most recent tick price for each product."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (product) product, price
        FROM price_tick_log
        WHERE product = ANY(%s)
        ORDER BY product, ts DESC
    """, (products,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[0]: float(r[1]) for r in rows}


def insert_shadow_trade(trade):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO shadow_trades
            (strategy, product, side, status, entry_ts, entry_price,
             position_size, peak_price, sl_pct, tp_pct, trail_pct,
             entry_rsi, entry_macd_hist, entry_adx, entry_atr, entry_vix, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        trade["strategy"], trade["product"], trade["side"], trade["status"],
        trade["entry_ts"], trade["entry_price"], trade["position_size"],
        trade["peak_price"], trade["sl_pct"], trade["tp_pct"], trade["trail_pct"],
        trade.get("entry_rsi"), trade.get("entry_macd_hist"),
        trade.get("entry_adx"), trade.get("entry_atr"),
        trade.get("entry_vix"), trade.get("notes"),
    ))
    trade_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return trade_id


def close_shadow_trade(trade_id, exit_price, exit_ts, exit_reason, pnl_dollars, pnl_pct):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE shadow_trades
        SET status='closed', exit_price=%s, exit_ts=%s, exit_reason=%s,
            pnl_dollars=%s, pnl_pct=%s
        WHERE id=%s
    """, (exit_price, exit_ts, exit_reason, pnl_dollars, pnl_pct, trade_id))
    conn.commit()
    cur.close()
    conn.close()


def update_peak_price(trade_id, new_peak):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE shadow_trades SET peak_price=%s WHERE id=%s", (new_peak, trade_id))
    conn.commit()
    cur.close()
    conn.close()


def get_open_shadow_trades(strategy=None):
    conn = get_conn()
    cur = conn.cursor()
    if strategy:
        cur.execute(
            "SELECT * FROM shadow_trades WHERE status='open' AND strategy=%s ORDER BY entry_ts",
            (strategy,))
    else:
        cur.execute("SELECT * FROM shadow_trades WHERE status='open' ORDER BY entry_ts")
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_closed_shadow_trades_since(ts):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM shadow_trades WHERE status='closed' AND exit_ts >= %s ORDER BY exit_ts",
        (ts,))
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_all_closed_shadow_trades():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM shadow_trades WHERE status='closed' ORDER BY exit_ts")
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_shadow_balance(strategy=None):
    conn = get_conn()
    cur = conn.cursor()
    if strategy:
        cur.execute(
            "SELECT balance FROM shadow_balance WHERE strategy=%s ORDER BY id DESC LIMIT 1",
            (strategy,))
    else:
        cur.execute(
            "SELECT balance FROM shadow_balance WHERE strategy IS NULL ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO shadow_balance (ts, balance, event, strategy) VALUES (%s, %s, %s, %s)",
            (datetime.now(timezone.utc), 100.0, "init", strategy))
        conn.commit()
        cur.close()
        conn.close()
        return 100.0
    cur.close()
    conn.close()
    return float(row[0])


def update_shadow_balance(balance, event, strategy=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shadow_balance (ts, balance, event, strategy) VALUES (%s, %s, %s, %s)",
        (datetime.now(timezone.utc), balance, event, strategy))
    conn.commit()
    cur.close()
    conn.close()


def get_all_strategy_balances():
    """Get latest balance for each strategy."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (strategy) strategy, balance
        FROM shadow_balance
        WHERE strategy IS NOT NULL
        ORDER BY strategy, id DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[0]: float(r[1]) for r in rows}


def get_full_dataset(product, granularity):
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT o.ts, o.open, o.high, o.low, o.close, o.volume,
               i.rsi_14, i.macd, i.macd_signal, i.macd_hist,
               i.bb_upper, i.bb_middle, i.bb_lower,
               i.ema_50, i.ema_200, i.atr_14, i.adx_14,
               i.obv, i.stoch_rsi, i.vwap
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
                "bb_upper", "bb_middle", "bb_lower",
                "ema_50", "ema_200", "atr_14", "adx_14",
                "obv", "stoch_rsi", "vwap"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def upsert_deribit_options(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO deribit_options
            (ts, currency, instrument_name, expiry_ts, strike, option_type,
             mark_iv, bid_iv, ask_iv, delta, gamma, vega, theta,
             open_interest, volume, mark_price, underlying_price)
        VALUES %s
        ON CONFLICT (instrument_name, ts)
        DO UPDATE SET mark_iv=EXCLUDED.mark_iv, bid_iv=EXCLUDED.bid_iv,
                      ask_iv=EXCLUDED.ask_iv, delta=EXCLUDED.delta,
                      gamma=EXCLUDED.gamma, vega=EXCLUDED.vega,
                      theta=EXCLUDED.theta, open_interest=EXCLUDED.open_interest,
                      volume=EXCLUDED.volume, mark_price=EXCLUDED.mark_price,
                      underlying_price=EXCLUDED.underlying_price
    """, [(
        r["ts"], r["currency"], r["instrument_name"], r["expiry_ts"],
        r["strike"], r["option_type"],
        r.get("mark_iv"), r.get("bid_iv"), r.get("ask_iv"),
        r.get("delta"), r.get("gamma"), r.get("vega"), r.get("theta"),
        r.get("open_interest"), r.get("volume"),
        r.get("mark_price"), r.get("underlying_price"),
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def upsert_deribit_surface(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO deribit_surface
            (ts, currency, expiry_ts, days_to_exp, atm_iv, skew_25d,
             pc_oi_ratio, total_oi, total_volume)
        VALUES %s
        ON CONFLICT (currency, expiry_ts, ts)
        DO UPDATE SET days_to_exp=EXCLUDED.days_to_exp, atm_iv=EXCLUDED.atm_iv,
                      skew_25d=EXCLUDED.skew_25d, pc_oi_ratio=EXCLUDED.pc_oi_ratio,
                      total_oi=EXCLUDED.total_oi, total_volume=EXCLUDED.total_volume
    """, [(
        r["ts"], r["currency"], r["expiry_ts"], r.get("days_to_exp"),
        r.get("atm_iv"), r.get("skew_25d"), r.get("pc_oi_ratio"),
        r.get("total_oi"), r.get("total_volume"),
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def upsert_macro_daily(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO macro_daily (ts, symbol, open, high, low, close, volume)
        VALUES %s
        ON CONFLICT (symbol, ts)
        DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                      close=EXCLUDED.close, volume=EXCLUDED.volume
    """, [(
        r["ts"], r["symbol"],
        r.get("open"), r.get("high"), r.get("low"), r["close"], r.get("volume"),
    ) for r in rows])
    conn.commit()
    cur.close()
    conn.close()


def get_latest_macro_ts(symbol):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(ts) FROM macro_daily WHERE symbol = %s", (symbol,))
    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


def get_latest_backtest_ts():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(run_ts) FROM backtest_results")
    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


def get_poor_backtest_combinations(threshold_pct=-10.0):
    """Return set of (product, granularity, strategy_name) from the latest backtest run
    where total_return < threshold_pct. Used to prune shadow strategies."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT product, granularity, strategy
        FROM backtest_results
        WHERE run_ts = (SELECT MAX(run_ts) FROM backtest_results)
          AND total_return < %s
    """, (threshold_pct,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {(r[0], r[1], r[2]) for r in rows}


def save_checkin_snapshot(balances):
    """Store current strategy balances as a check-in snapshot."""
    if not balances:
        return
    ts = datetime.now(timezone.utc)
    conn = get_conn()
    cur = conn.cursor()
    execute_values(cur,
        "INSERT INTO shadow_checkin_snapshot (ts, strategy, balance) VALUES %s",
        [(ts, strategy, balance) for strategy, balance in balances.items()]
    )
    conn.commit()
    cur.close()
    conn.close()


def get_last_checkin_snapshot():
    """Return (ts, {strategy: balance}) from the most recent prior check-in snapshot."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT ts, strategy, balance
        FROM shadow_checkin_snapshot
        WHERE ts = (SELECT MAX(ts) FROM shadow_checkin_snapshot)
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return None, {}
    return rows[0][0], {r[1]: float(r[2]) for r in rows}


def get_macro_context(products):
    """Fetch latest VIX and 1day EMA 200 per product for regime filters.
    Returns {"vix": float|None, "ema200": {product: {"ema200": float, "close": float}}}.
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT close FROM macro_daily WHERE symbol = '^VIX' ORDER BY ts DESC LIMIT 1"
    )
    row = cur.fetchone()
    vix = float(row[0]) if row else None

    ema200 = {}
    for product in products:
        cur.execute("""
            SELECT i.ema_200, o.close
            FROM indicators i
            JOIN ohlcv o ON i.product = o.product AND i.ts = o.ts AND i.granularity = o.granularity
            WHERE i.product = %s AND i.granularity = '1day' AND i.ema_200 IS NOT NULL
            ORDER BY i.ts DESC LIMIT 1
        """, (product,))
        row = cur.fetchone()
        if row:
            ema200[product] = {"ema200": float(row[0]), "close": float(row[1])}

    cur.close()
    conn.close()
    return {"vix": vix, "ema200": ema200}
