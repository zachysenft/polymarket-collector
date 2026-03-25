import logging
import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler

from db import init_schema, insert_poly_snapshots, insert_kalshi_snapshots
from btc_collector import BTCPriceCollector, funding_rate_loop
from poly_collector import snapshot_crypto_markets, snapshot_kalshi_markets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def poly_snapshot_job():
    try:
        rows = snapshot_crypto_markets()
        insert_poly_snapshots(rows)
    except Exception as e:
        log.error(f"Polymarket snapshot job failed: {e}")


def kalshi_snapshot_job():
    try:
        rows = snapshot_kalshi_markets()
        insert_kalshi_snapshots(rows)
    except Exception as e:
        log.error(f"Kalshi snapshot job failed: {e}")


def main():
    log.info("=" * 60)
    log.info("  Polymarket Lag Research Collector — Starting Up")
    log.info("=" * 60)

    # Initialize DB schema
    init_schema()

    # ── Thread 1: BTC price via websocket (logs every 60s) ──
    btc_collector = BTCPriceCollector(log_interval_seconds=60)
    ws_thread = threading.Thread(
        target=btc_collector.start,
        daemon=True,
        name="btc-websocket"
    )
    ws_thread.start()
    log.info("BTC websocket thread started")

    # ── Thread 2: Funding rate every 5 min ──
    funding_thread = threading.Thread(
        target=funding_rate_loop,
        args=(300,),
        daemon=True,
        name="funding-rate"
    )
    funding_thread.start()
    log.info("Funding rate thread started")

    # ── Scheduler: Polymarket snapshot every 2 min ──
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        poly_snapshot_job,
        "interval",
        minutes=2,
        id="poly_snapshot",
        max_instances=1,        # never overlap
        misfire_grace_time=30
    )
    scheduler.add_job(
        kalshi_snapshot_job,
        "interval",
        minutes=2,
        id="kalshi_snapshot",
        max_instances=1,
        misfire_grace_time=30
    )
    scheduler.start()
    log.info("Polymarket + Kalshi snapshot schedulers started (every 2 min)")

    # Run one snapshot immediately so we don't wait 2 min on startup
    log.info("Running initial snapshots...")
    poly_snapshot_job()
    kalshi_snapshot_job()

    log.info("All collectors running. Ctrl+C to stop.")

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
            log.info("Heartbeat — collector running")
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
