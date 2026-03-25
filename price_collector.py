import json
import time
import logging
from datetime import datetime, timezone
import websocket
from db import insert_price_tick

log = logging.getLogger(__name__)

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]


class PriceCollector:
    def __init__(self, log_interval_seconds=60):
        self.log_interval = log_interval_seconds
        self.last_logged = {p: 0 for p in PRODUCTS}
        self.last_price = {p: None for p in PRODUCTS}
        self.ws = None

    def on_open(self, ws):
        log.info("Coinbase websocket connected")
        ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": PRODUCTS,
            "channel": "ticker"
        }))

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") != "ticker":
                return
            for event in data.get("events", []):
                for ticker in event.get("tickers", []):
                    product = ticker.get("product_id")
                    if product not in self.last_logged:
                        continue
                    price = float(ticker.get("price", 0))
                    if price == 0:
                        continue

                    self.last_price[product] = price
                    now = time.time()
                    if now - self.last_logged[product] >= self.log_interval:
                        insert_price_tick(datetime.now(timezone.utc), product, price)
                        log.info(f"{product}: ${price:,.2f}")
                        self.last_logged[product] = now
        except Exception as e:
            log.error(f"Websocket message error: {e}")

    def on_error(self, ws, error):
        log.error(f"Websocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        log.warning("Websocket closed. Reconnecting in 5s...")
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
