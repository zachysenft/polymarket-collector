import logging
import ta
from db import get_ohlcv_df, upsert_indicators

log = logging.getLogger(__name__)


def compute_and_store(product, granularity, bulk=False):
    """
    Fetch OHLCV data, compute RSI/MACD/BB, upsert to DB.
    bulk=True: compute + store ALL valid rows (for backfill).
    bulk=False: only store most recent 10 rows (for ongoing updates).
    """
    limit = 10000 if bulk else 200
    df = get_ohlcv_df(product, granularity, limit=limit)
    if len(df) < 30:
        log.warning(f"Not enough data for {product} {granularity} ({len(df)} rows, need 30+)")
        return

    # RSI(14)
    df["rsi_14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

    # MACD(12, 26, 9)
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # Bollinger Bands(20, 2)
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_middle"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()

    # Filter to rows with valid indicator values
    valid = df.dropna(subset=["rsi_14", "macd", "bb_upper"])
    if not bulk:
        valid = valid.tail(10)
    if valid.empty:
        return

    rows = []
    for _, r in valid.iterrows():
        rows.append({
            "product":     product,
            "ts":          r["ts"],
            "granularity": granularity,
            "rsi_14":      round(float(r["rsi_14"]), 4),
            "macd":        round(float(r["macd"]), 6),
            "macd_signal": round(float(r["macd_signal"]), 6),
            "macd_hist":   round(float(r["macd_hist"]), 6),
            "bb_upper":    round(float(r["bb_upper"]), 4),
            "bb_middle":   round(float(r["bb_middle"]), 4),
            "bb_lower":    round(float(r["bb_lower"]), 4),
        })

    # Batch in chunks of 500 to avoid huge single queries
    for i in range(0, len(rows), 500):
        upsert_indicators(rows[i:i+500])

    log.info(f"Indicators computed for {product} {granularity} ({len(rows)} rows)")
