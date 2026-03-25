import json
import time
import logging
from datetime import datetime, timezone
import websocket
import requests
from db import insert_btc_price, insert_funding_rate

log = logging.getLogger(__name__)

COINBASE_WS  = "wss://advanced-trade-ws.coinbase.com"
OKX_REST     = "https://www.okx.com"


def fetch_funding_rate():
    try:
        r = requests.get(
            f"{OKX_REST}/api/v5/public/funding-rate",
            params={"instId": "BTC-USD-SWAP"},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        entry = data["data"][0]
        ts = datetime.now(timezone.utc)
        rate = float(entry["fundingRate"])
        insert_funding_rate(ts, rate)
        log.info(f"Funding rate (OKX): {rate:.6%}")
    except Exception as e:
        log.error(f"Funding rate fetch error: {e}")


def funding_rate_loop(interval_seconds=300):
    while True:
        fetch_funding_rate()
        time.sleep(interval_seconds)


class BTCPriceCollector:
    def __init__(self, log_interval_seconds=60):
        self.log_interval = log_interval_seconds
        self.last_logged  = 0
        self.last_price   = None
        self.ws           = None

    def on_open(self, ws):
        log.info("Coinbase websocket connected")
        ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "ticker"
        }))

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") != "ticker":
                return
            for event in data.get("events", []):
                for ticker in event.get("tickers", []):
                    price = float(ticker.get("price", 0))
                    if price == 0:
                        continue
                    self.last_price = price
                    now = time.time()
                    if now - self.last_logged >= self.log_interval:
                        insert_btc_price(datetime.now(timezone.utc), price)
                        log.info(f"BTC: ${price:,.2f}")
                        self.last_logged = now
        except Exception as e:
            log.error(f"BTC websocket message error: {e}")

    def on_error(self, ws, error):
        log.error(f"BTC websocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        log.warning(f"BTC websocket closed. Reconnecting in 5s...")
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
