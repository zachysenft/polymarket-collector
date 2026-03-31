import logging
import time
from datetime import timedelta, date

import requests as _requests
import yfinance as yf

from db import upsert_macro_daily, get_latest_macro_ts

log = logging.getLogger(__name__)

SYMBOLS = ["SPY", "QQQ", "^VIX"]

# Yahoo Finance blocks cloud server IPs by default — a browser User-Agent bypasses it
_SESSION = _requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})


def _fetch_symbol(symbol, start_date, end_date):
    """Fetch daily OHLCV for a symbol between start and end dates. Retries on rate limit."""
    for attempt in range(3):
        try:
            ticker = yf.Ticker(symbol, session=_SESSION)
            hist = ticker.history(start=start_date.isoformat(), end=end_date.isoformat())
            break
        except Exception as e:
            if attempt < 2:
                wait = 5 * (2 ** attempt)  # 5s, 10s
                log.warning(f"Macro {symbol}: fetch error ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
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

    for i, symbol in enumerate(SYMBOLS):
        if i > 0:
            time.sleep(3)
        try:
            latest = get_latest_macro_ts(symbol)
            if latest:
                # Skip if data is fresh (within 3 days covers weekends + same-day redeploys)
                if (date.today() - latest).days <= 3:
                    log.info(f"Macro {symbol}: already up to date ({latest})")
                    continue
                start_date = latest + timedelta(days=1)
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

    for i, symbol in enumerate(SYMBOLS):
        if i > 0:
            time.sleep(3)
        try:
            rows = _fetch_symbol(symbol, start_date, end_date)
            if rows:
                upsert_macro_daily(rows)
                total += len(rows)
        except Exception as e:
            log.error(f"Macro daily collection failed for {symbol}: {e}")

    log.info(f"Macro daily collection complete — {total} rows upserted")


def check_vix_regime_change():
    """Check if VIX crossed the 25 threshold since yesterday. Send Discord alert if so."""
    from db import get_conn
    from discord_bot import send_regime_alert
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT close FROM macro_daily
            WHERE symbol = '^VIX'
            ORDER BY ts DESC LIMIT 2
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if len(rows) < 2:
            return
        vix_today = float(rows[0][0])
        vix_yesterday = float(rows[1][0])
        send_regime_alert(vix_today, vix_yesterday)
    except Exception as e:
        log.error(f"VIX regime check failed: {e}")
