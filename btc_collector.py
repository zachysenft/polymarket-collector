import json
import time
import logging
import threading
from datetime import datetime, timezone
import websocket
import requests
from db import insert_btc_price, insert_funding_rate

log = logging.getLogger(__name__)

# Coinbase websocket — US accessible, no auth needed for ticker
COINBASE_WS   = "wss://advanced-trade-ws.coinbase.com"
# Bybit — US accessible for funding rate data
BYBIT_REST    = "https://api.bybit.com"


# ─── Funding Rate via Bybit (REST, every 5 min) ──────────────────────────────

def fetch_funding_rate():
    try:
        r = requests.get(
            f"{BYBIT_REST}/v5/market/funding/history",
            params={"category": "linear", "symbol": "BTCUSDT", "limit": 1},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        entry = data["result"]["list"][0]
        ts = datetime.now(timezone.utc)
        rate = float(entry["fundingRate"])
        insert_funding_rate(ts, rate)
        log.info(f"Funding rate (Bybit): {rate:.6%}")
    except Exception as e:
        log.error(f"Funding rate fetch error: {e}")


def funding_rate_loop(interval_seconds=300):
    """Runs in a background thread, polls every 5 minutes."""
    while True:
        fetch_funding_rate()
        time.sleep(interval_seconds)


# ─── BTC Price via Coinbase Websocket ────────────────────────────────────────

class BTCPriceCollector:
    def __init__(self, log_interval_seconds=60):
        self.log_interval = log_interval_seconds
        self.last_logged  = 0
        self.last_price   = None
        self.ws           = None

    def on_open(self, ws):
        log.info("Coinbase websocket connected")
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "ticker"
        })
        ws.send(subscribe_msg)

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            # Coinbase Advanced Trade sends events with a list of tickers
            if data.get("channel") != "ticker":
                return
            events = data.get("events", [])
            for event in events:
                for ticker in event.get("tickers", []):
                    price = float(ticker.get("price", 0))
                    if price == 0:
                        continue
                    self.last_price = price
                    now = time.time()
                    if now - self.last_logged >= self.log_interval:
                        ts = datetime.now(timezone.utc)
                        insert_btc_price(ts, price)
                        log.info(f"BTC: ${price:,.2f}")
                        self.last_logged = now
        except Exception as e:
            log.error(f"BTC websocket message error: {e}")

    def on_error(self, ws, error):
        log.error(f"BTC websocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        log.warning(f"BTC websocket closed ({close_status_code}). Reconnecting in 5s...")
        time.sleep(5)
        self.start()

    def start(self):
        self.ws = websocket.WebSocketApp(
            COINBASE_WS,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)
