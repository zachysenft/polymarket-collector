import logging
import pandas_ta as ta
from db import get_ohlcv_df, upsert_indicators

log = logging.getLogger(__name__)


def compute_and_store(product, granularity):
    """
    Fetch recent OHLCV data, compute RSI/MACD/BB, upsert latest rows to DB.
    """
    df = get_ohlcv_df(product, granularity, limit=200)
    if len(df) < 30:
        log.warning(f"Not enough data for {product} {granularity} ({len(df)} rows, need 30+)")
        return

    # Compute indicators
    df["rsi_14"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"] = macd["MACDh_12_26_9"]

    bb = ta.bbands(df["close"], length=20, std=2)
    df["bb_upper"] = bb["BBU_20_2.0"]
    df["bb_middle"] = bb["BBM_20_2.0"]
    df["bb_lower"] = bb["BBL_20_2.0"]

    # Only upsert the most recent 10 rows that have valid indicator values
    valid = df.dropna(subset=["rsi_14", "macd", "bb_upper"]).tail(10)
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

    upsert_indicators(rows)
    log.debug(f"Indicators computed for {product} {granularity} ({len(rows)} rows)")
