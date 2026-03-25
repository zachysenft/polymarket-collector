import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
import websocket
import requests
from db import insert_btc_price, insert_funding_rate

log = logging.getLogger(__name__)

BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
BINANCE_REST = "https://fapi.binance.com"


# ─── Funding Rate (REST, every 5 min) ────────────────────────────────────────

def fetch_funding_rate():
    try:
        r = requests.get(
            f"{BINANCE_REST}/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        ts = datetime.now(timezone.utc)
        rate = float(data["lastFundingRate"])
        next_ts = datetime.fromtimestamp(
            int(data["nextFundingTime"]) / 1000, tz=timezone.utc
        )
        insert_funding_rate(ts, rate, next_ts)
        log.info(f"Funding rate: {rate:.6%} | Next: {next_ts.strftime('%H:%M UTC')}")
    except Exception as e:
        log.error(f"Funding rate fetch error: {e}")


def funding_rate_loop(interval_seconds=300):
    """Runs in a background thread, polls every 5 minutes."""
    while True:
        fetch_funding_rate()
        time.sleep(interval_seconds)


# ─── BTC Price (Websocket, real-time) ────────────────────────────────────────

class BTCPriceCollector:
    def __init__(self, log_interval_seconds=60):
        self.log_interval = log_interval_seconds
        self.last_logged   = 0
        self.last_price    = None
        self.last_volume   = None
        self.ws            = None

    def on_message(self, ws, message):
        try:
            data  = json.loads(message)
            price = float(data["c"])   # current price
            vol   = float(data["v"])   # 24h volume in BTC
            self.last_price  = price
            self.last_volume = vol

            now = time.time()
            if now - self.last_logged >= self.log_interval:
                ts = datetime.now(timezone.utc)
                insert_btc_price(ts, price, vol)
                log.info(f"BTC: ${price:,.2f} | 24h vol: {vol:,.0f} BTC")
                self.last_logged = now

        except Exception as e:
            log.error(f"BTC websocket message error: {e}")

    def on_error(self, ws, error):
        log.error(f"BTC websocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        log.warning(f"BTC websocket closed ({close_status_code}). Reconnecting in 5s...")
        time.sleep(5)
        self.start()

    def on_open(self, ws):
        log.info("BTC websocket connected")

    def start(self):
        self.ws = websocket.WebSocketApp(
            BINANCE_WS,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open,
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)
