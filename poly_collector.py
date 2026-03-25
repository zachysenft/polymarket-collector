import logging
import time
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def get_active_btc_markets():
    """
    Fetch active BTC/crypto markets from Polymarket.
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

        btc_markets = [
            m for m in markets
            if any(kw in m.get("question", "").lower()
                   for kw in ["bitcoin", "btc", "crypto", "cryptocurrency"])
        ]

        log.info(f"Found {len(btc_markets)} active BTC markets")
        return btc_markets

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


def snapshot_btc_markets():
    """
    Take a full snapshot of all active BTC markets.
    Returns list of row dicts ready for DB insert.
    """
    markets = get_active_btc_markets()
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

    log.info(f"Polymarket snapshot: {len(rows)} BTC markets captured")
    return rows
