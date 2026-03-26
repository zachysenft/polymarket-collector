import logging
import numpy as np
import ta
from db import get_ohlcv_df, upsert_indicators

log = logging.getLogger(__name__)


def _safe_round(val, decimals):
    """Round a value, returning None if NaN."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    return round(float(val), decimals)


def compute_and_store(product, granularity, bulk=False):
    """
    Fetch OHLCV data, compute all indicators, upsert to DB.
    bulk=True: compute + store ALL valid rows (for backfill).
    bulk=False: only store most recent 10 rows (for ongoing updates).
    """
    limit = 10000 if bulk else 300
    df = get_ohlcv_df(product, granularity, limit=limit)
    if len(df) < 30:
        log.warning(f"Not enough data for {product} {granularity} ({len(df)} rows, need 30+)")
        return

    # --- Original indicators ---
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

    # --- New indicators ---
    # EMA 50 & 200 — trend direction, golden/death cross
    df["ema_50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema_200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    # ATR(14) — volatility, useful for dynamic SL/TP sizing
    df["atr_14"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=14).average_true_range()

    # ADX(14) — trend strength (>25 = trending, <20 = ranging)
    df["adx_14"] = ta.trend.ADXIndicator(
        df["high"], df["low"], df["close"], window=14).adx()

    # OBV — volume confirms price moves
    df["obv"] = ta.volume.OnBalanceVolumeIndicator(
        df["close"], df["volume"]).on_balance_volume()

    # Stochastic RSI — more sensitive than RSI, catches turns earlier
    df["stoch_rsi"] = ta.momentum.StochRSIIndicator(
        df["close"], window=14, smooth1=3, smooth2=3).stochrsi()

    # VWAP — institutional benchmark (cumulative within session)
    df["vwap"] = (df["volume"] * (df["high"] + df["low"] + df["close"]) / 3).cumsum() / df["volume"].cumsum()

    # Filter to rows with valid core indicator values
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
            "rsi_14":      _safe_round(r["rsi_14"], 4),
            "macd":        _safe_round(r["macd"], 6),
            "macd_signal": _safe_round(r["macd_signal"], 6),
            "macd_hist":   _safe_round(r["macd_hist"], 6),
            "bb_upper":    _safe_round(r["bb_upper"], 4),
            "bb_middle":   _safe_round(r["bb_middle"], 4),
            "bb_lower":    _safe_round(r["bb_lower"], 4),
            "ema_50":      _safe_round(r["ema_50"], 4),
            "ema_200":     _safe_round(r["ema_200"], 4),
            "atr_14":      _safe_round(r["atr_14"], 6),
            "adx_14":      _safe_round(r["adx_14"], 4),
            "obv":         _safe_round(r["obv"], 2),
            "stoch_rsi":   _safe_round(r["stoch_rsi"], 4),
            "vwap":        _safe_round(r["vwap"], 4),
        })

    # Batch in chunks of 500 to avoid huge single queries
    for i in range(0, len(rows), 500):
        upsert_indicators(rows[i:i+500])

    log.info(f"Indicators computed for {product} {granularity} ({len(rows)} rows)")
