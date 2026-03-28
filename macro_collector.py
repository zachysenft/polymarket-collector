import logging
from datetime import datetime, timezone, timedelta, date

import yfinance as yf

from db import upsert_macro_daily, get_latest_macro_ts

log = logging.getLogger(__name__)

SYMBOLS = ["SPY", "QQQ", "^VIX"]


def _fetch_symbol(symbol, start_date, end_date):
    """Fetch daily OHLCV for a symbol between start and end dates."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(start=start_date.isoformat(), end=end_date.isoformat())
    if hist.empty:
        return []
    rows = []
    for idx, row in hist.iterrows():
        # yfinance returns tz-aware index; normalize to date
        ts = idx.date() if hasattr(idx, "date") else idx
        volume = int(row["Volume"]) if row["Volume"] and row["Volume"] == row["Volume"] else None
        rows.append({
            "ts": ts,
            "symbol": symbol,
            "open": round(float(row["Open"]), 4) if row["Open"] == row["Open"] else None,
            "high": round(float(row["High"]), 4) if row["High"] == row["High"] else None,
            "low": round(float(row["Low"]), 4) if row["Low"] == row["Low"] else None,
            "close": round(float(row["Close"]), 4),
            "volume": volume,
        })
    return rows


def backfill_macro(days_back=90):
    """Backfill SPY/QQQ/VIX history. Resumes from latest stored date per symbol."""
    end_date = date.today() + timedelta(days=1)  # inclusive of today
    total = 0

    for symbol in SYMBOLS:
        try:
            latest = get_latest_macro_ts(symbol)
            if latest:
                # Resume from day after latest stored
                start_date = latest + timedelta(days=1)
                if start_date >= end_date:
                    log.info(f"Macro {symbol}: already up to date")
                    continue
            else:
                start_date = date.today() - timedelta(days=days_back)

            rows = _fetch_symbol(symbol, start_date, end_date)
            if rows:
                upsert_macro_daily(rows)
                total += len(rows)
                log.info(f"Macro {symbol}: backfilled {len(rows)} days")
            else:
                log.info(f"Macro {symbol}: no new data")
        except Exception as e:
            log.error(f"Macro backfill failed for {symbol}: {e}")

    log.info(f"Macro backfill complete — {total} rows stored")


def collect_macro_daily():
    """Fetch last 5 trading days for each macro symbol (handles weekends/holidays)."""
    end_date = date.today() + timedelta(days=1)
    start_date = date.today() - timedelta(days=5)
    total = 0

    for symbol in SYMBOLS:
        try:
            rows = _fetch_symbol(symbol, start_date, end_date)
            if rows:
                upsert_macro_daily(rows)
                total += len(rows)
        except Exception as e:
            log.error(f"Macro daily collection failed for {symbol}: {e}")

    log.info(f"Macro daily collection complete — {total} rows upserted")
