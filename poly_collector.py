import logging
import time
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

CLOB_BASE   = "https://clob.polymarket.com"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
KALSHI_BASE = "https://trading-api.kalshi.com/trade-api/v2"


CRYPTO_KEYWORDS = [
    "bitcoin", "btc",
    "ethereum", "eth",
    "solana", "sol",
    "ripple", "xrp",
]


def get_active_crypto_markets():
    """
    Fetch active BTC/ETH/SOL/XRP markets from Polymarket.
    Returns list of dicts with market metadata.
    """
    try:
        r = requests.get(
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
            },
            timeout=15
        )
        r.raise_for_status()
        markets = r.json()

        crypto_markets = [
            m for m in markets
            if any(kw in m.get("question", "").lower() for kw in CRYPTO_KEYWORDS)
        ]

        log.info(f"Found {len(crypto_markets)} active crypto markets")
        return crypto_markets

    except Exception as e:
        log.error(f"Error fetching Polymarket markets: {e}")
        return []


def get_market_prices(condition_id):
    """
    Fetch current yes/no prices for a market from the CLOB.
    Returns (yes_price, no_price) or (None, None) on error.
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/markets/{condition_id}",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()

        tokens = data.get("tokens", [])
        if len(tokens) < 2:
            return None, None

        yes_price = float(tokens[0]["price"])
        no_price  = float(tokens[1]["price"])
        return yes_price, no_price

    except Exception as e:
        log.error(f"Error fetching price for {condition_id}: {e}")
        return None, None


def snapshot_crypto_markets():
    """
    Take a full snapshot of all active BTC/ETH/SOL/XRP markets.
    Returns list of row dicts ready for DB insert.
    """
    markets = get_active_crypto_markets()
    rows = []
    ts = datetime.now(timezone.utc)

    for m in markets:
        condition_id = m.get("conditionId") or m.get("condition_id")
        if not condition_id:
            continue

        time.sleep(0.5)  # avoid rate limiting
        yes_price, no_price = get_market_prices(condition_id)
        if yes_price is None:
            continue

        end_date = None
        if m.get("endDate") or m.get("end_date_iso"):
            try:
                raw = m.get("endDate") or m.get("end_date_iso")
                end_date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                pass

        row = {
            "ts":        ts,
            "market_id": condition_id,
            "question":  m.get("question", "")[:500],
            "yes_price": yes_price,
            "no_price":  no_price,
            "volume":    m.get("volume"),
            "end_date":  end_date,
        }
        rows.append(row)
        log.debug(f"  {row['question'][:60]} | YES: {yes_price:.3f} | NO: {no_price:.3f}")

    log.info(f"Polymarket snapshot: {len(rows)} crypto markets captured")
    return rows


# ── Kalshi ────────────────────────────────────────────────────────────────────

def kalshi_get_active_crypto_markets():
    """
    Fetch open BTC/ETH/SOL/XRP markets from Kalshi.
    Returns list of market dicts.
    """
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params={
                "status": "open",
                "limit":  1000,
            },
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        markets = data.get("markets", [])

        crypto_markets = [
            m for m in markets
            if any(kw in m.get("title", "").lower() for kw in CRYPTO_KEYWORDS)
        ]

        log.info(f"Kalshi: found {len(crypto_markets)} open crypto markets")
        return crypto_markets

    except Exception as e:
        log.error(f"Error fetching Kalshi markets: {e}")
        return []


def snapshot_kalshi_markets():
    """
    Take a full snapshot of all open BTC/ETH/SOL/XRP Kalshi markets.
    Returns list of row dicts ready for DB insert.
    Kalshi prices are in cents (0–99); converted to 0–1 probability.
    """
    markets = kalshi_get_active_crypto_markets()
    rows = []
    ts = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker")
        if not ticker:
            continue

        yes_bid = m.get("yes_bid")
        yes_ask = m.get("yes_ask")
        no_bid  = m.get("no_bid")
        no_ask  = m.get("no_ask")

        if yes_bid is None or yes_ask is None:
            continue

        yes_price = (yes_bid + yes_ask) / 2 / 100
        no_price  = (
            (no_bid + no_ask) / 2 / 100
            if (no_bid is not None and no_ask is not None)
            else round(1 - yes_price, 4)
        )

        end_date = None
        if m.get("close_time"):
            try:
                end_date = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            except Exception:
                pass

        row = {
            "ts":        ts,
            "market_id": ticker,
            "question":  m.get("title", "")[:500],
            "yes_price": yes_price,
            "no_price":  no_price,
            "volume":    m.get("volume"),
            "end_date":  end_date,
        }
        rows.append(row)
        log.debug(f"  {row['question'][:60]} | YES: {yes_price:.3f} | NO: {no_price:.3f}")

    log.info(f"Kalshi snapshot: {len(rows)} crypto markets captured")
    return rows
