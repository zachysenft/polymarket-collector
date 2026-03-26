import logging
import time
import requests
from datetime import datetime, timezone, timedelta

from db import upsert_ohlcv, get_latest_ohlcv_ts, get_ohlcv_df
from indicators import compute_and_store

log = logging.getLogger(__name__)

COINBASE_BASE = "https://api.exchange.coinbase.com"
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]
GRANULARITIES = [300, 3600]  # 5-min, 1-hour
MAX_CANDLES = 300

GRAN_LABELS = {300: "5min", 3600: "1hour"}


def fetch_candles(product, granularity_secs, start_dt, end_dt):
    """
    Fetch OHLCV candles from the public Coinbase Exchange API.
    Returns list of row dicts ready for DB upsert.
    """
    try:
        r = requests.get(
            f"{COINBASE_BASE}/products/{product}/candles",
            params={
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "granularity": granularity_secs,
            },
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"Error fetching candles for {product} ({GRAN_LABELS.get(granularity_secs)}): {e}")
        return []

    rows = []
    gran_label = GRAN_LABELS.get(granularity_secs, str(granularity_secs))
    for candle in data:
        # Coinbase format: [time, low, high, open, close, volume]
        ts = datetime.fromtimestamp(candle[0], tz=timezone.utc)
        rows.append({
            "product":     product,
            "ts":          ts,
            "granularity": gran_label,
            "open":        float(candle[3]),
            "high":        float(candle[2]),
            "low":         float(candle[1]),
            "close":       float(candle[4]),
            "volume":      float(candle[5]),
        })
    return rows


def backfill(product, granularity_secs, days_back=30):
    """
    Backfill historical candles for one product/granularity.
    Paginates backward from now, skips data already in DB.
    """
    gran_label = GRAN_LABELS.get(granularity_secs, str(granularity_secs))
    latest_ts = get_latest_ohlcv_ts(product, gran_label)

    end_dt = datetime.now(timezone.utc)
    cutoff = end_dt - timedelta(days=days_back)

    if latest_ts and latest_ts > cutoff:
        cutoff = latest_ts
        log.info(f"  {product} {gran_label}: resuming from {cutoff.isoformat()}")

    total_rows = 0
    while end_dt > cutoff:
        window = timedelta(seconds=granularity_secs * MAX_CANDLES)
        start_dt = max(end_dt - window, cutoff)

        rows = fetch_candles(product, granularity_secs, start_dt, end_dt)
        if rows:
            upsert_ohlcv(rows)
            total_rows += len(rows)

        end_dt = start_dt
        time.sleep(0.3)

    log.info(f"  {product} {gran_label}: backfilled {total_rows} candles")
    return total_rows


def run_backfill_all(days_back=30):
    """Run backfill for all products and granularities."""
    log.info(f"Starting backfill ({days_back} days)...")
    total = 0
    for gran in GRANULARITIES:
        for product in PRODUCTS:
            total += backfill(product, gran, days_back)
    log.info(f"Backfill complete: {total} total candles")
    return total


def collect_all_products(granularity_secs):
    """
    Fetch latest candles for all products, upsert, compute indicators.
    Called on a schedule (every 5 min for 5-min candles, every hour for 1-hour).
    """
    gran_label = GRAN_LABELS.get(granularity_secs, str(granularity_secs))
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(seconds=granularity_secs * 10)

    for product in PRODUCTS:
        rows = fetch_candles(product, granularity_secs, start_dt, end_dt)
        if rows:
            upsert_ohlcv(rows)

        try:
            compute_and_store(product, gran_label)
        except Exception as e:
            log.error(f"Indicator computation failed for {product} {gran_label}: {e}")

        time.sleep(0.2)

    log.info(f"Collected {gran_label} candles for all products")


def aggregate_daily_candles():
    """
    Roll up 1-hour candles into 1-day candles.
    Groups by product + UTC date, computes OHLCV, upserts as granularity='1day'.
    """
    import pandas as pd

    for product in PRODUCTS:
        df = get_ohlcv_df(product, "1hour", limit=10000)
        if df.empty or len(df) < 24:
            continue

        df["date"] = pd.to_datetime(df["ts"]).dt.date
        daily = df.groupby("date").agg(
            ts=("ts", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).reset_index(drop=True)

        # Only keep full days (24 hourly candles) except today (partial is fine)
        hour_counts = df.groupby("date").size()
        today = datetime.now(timezone.utc).date()
        full_days = hour_counts[(hour_counts >= 24) | (hour_counts.index == today)].index
        daily = daily[pd.to_datetime(daily["ts"]).dt.date.isin(full_days)]

        if daily.empty:
            continue

        rows = []
        for _, r in daily.iterrows():
            rows.append({
                "product":     product,
                "ts":          r["ts"],
                "granularity": "1day",
                "open":        float(r["open"]),
                "high":        float(r["high"]),
                "low":         float(r["low"]),
                "close":       float(r["close"]),
                "volume":      float(r["volume"]),
            })

        upsert_ohlcv(rows)

        try:
            compute_and_store(product, "1day", bulk=True)
        except Exception as e:
            log.error(f"Daily indicator computation failed for {product}: {e}")

    log.info(f"Daily candles aggregated for all products")
