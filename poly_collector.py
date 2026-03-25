import json
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

CRYPTO_KEYWORDS = [
    "bitcoin", "btc",
    "ethereum", "eth",
    "solana", "sol",
    "ripple", "xrp",
]


def get_active_crypto_markets():
    """
    Fetch active BTC/ETH/SOL/XRP markets from Polymarket Gamma API.
    Returns list of dicts with market metadata including prices.
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


def parse_gamma_prices(m):
    """
    Extract yes/no prices from Gamma API market data.
    Gamma returns outcomePrices as a JSON string e.g. '["0.45", "0.55"]'.
    Returns (yes_price, no_price) or (None, None) if unavailable.
    """
    try:
        raw = m.get("outcomePrices")
        if raw:
            prices = json.loads(raw) if isinstance(raw, str) else raw
            if len(prices) >= 2:
                return float(prices[0]), float(prices[1])
    except Exception:
        pass
    return None, None


def snapshot_crypto_markets():
    """
    Take a full snapshot of all active BTC/ETH/SOL/XRP markets.
    Prices sourced from Gamma API — no CLOB calls, no rate limiting.
    Returns list of row dicts ready for DB insert.
    """
    markets = get_active_crypto_markets()
    rows = []
    ts = datetime.now(timezone.utc)

    for m in markets:
        condition_id = m.get("conditionId") or m.get("condition_id")
        if not condition_id:
            continue

        yes_price, no_price = parse_gamma_prices(m)
        if yes_price is None:
            log.warning(f"No price data for market {condition_id[:16]}… — skipping")
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
