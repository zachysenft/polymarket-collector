import logging
import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler

from db import init_schema
from price_collector import PriceCollector
from ohlcv_collector import run_backfill_all, collect_all_products, aggregate_daily_candles, PRODUCTS, GRANULARITIES, GRAN_LABELS
from indicators import compute_and_store
from backtester import run_all_backtests, run_param_sweep
from discord_bot import send_startup_message, send_backtest_summary, send_trade_breakdown, start_discord_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def ohlcv_5min_job():
    try:
        collect_all_products(300)
    except Exception as e:
        log.error(f"5-min OHLCV job failed: {e}")


def ohlcv_1hour_job():
    try:
        collect_all_products(3600)
    except Exception as e:
        log.error(f"1-hour OHLCV job failed: {e}")


def daily_backtest_job():
    """Re-run all backtests + param sweep with accumulated data. Runs once per day."""
    try:
        log.info("=" * 60)
        log.info("  DAILY BACKTEST RE-RUN — using all accumulated data")
        log.info("=" * 60)
        # Aggregate daily candles from hourly data
        aggregate_daily_candles()
        # Recompute indicators in bulk so backtests use latest data
        for gran in GRANULARITIES:
            gran_label = GRAN_LABELS[gran]
            for product in PRODUCTS:
                try:
                    compute_and_store(product, gran_label, bulk=True)
                except Exception as e:
                    log.error(f"Daily indicator recompute failed for {product} {gran_label}: {e}")
        bt_results = run_all_backtests()
        sweep_results = run_param_sweep()
        try:
            send_backtest_summary(bt_results, sweep_results)
            send_trade_breakdown(bt_results)
        except Exception as e:
            log.error(f"Discord notification failed: {e}")
        log.info("Daily backtest complete")
    except Exception as e:
        log.error(f"Daily backtest job failed: {e}")


def main():
    log.info("=" * 60)
    log.info("  Crypto Data Aggregator — Starting Up")
    log.info("=" * 60)

    # 0. Send Discord startup notification
    try:
        send_startup_message()
    except Exception as e:
        log.warning(f"Discord startup message failed: {e}")

    # 1. Initialize DB schema
    init_schema()

    # 2. Backfill 30 days of historical OHLCV data
    run_backfill_all(days_back=30)

    # 3. Aggregate daily candles from hourly data
    aggregate_daily_candles()

    # 4. Compute indicators on all backfilled data
    log.info("Computing indicators on backfilled data...")
    for gran in GRANULARITIES:
        gran_label = GRAN_LABELS[gran]
        for product in PRODUCTS:
            try:
                compute_and_store(product, gran_label, bulk=True)
            except Exception as e:
                log.error(f"Indicator computation failed for {product} {gran_label}: {e}")
    log.info("Indicators computed")

    # 5. Run backtests (default params)
    bt_results = run_all_backtests()

    # 6. Run parameter sweep (multiple SL/TP/trail configs)
    sweep_results = run_param_sweep()

    # 6b. Send initial backtest results to Discord
    try:
        send_backtest_summary(bt_results, sweep_results)
        send_trade_breakdown(bt_results)
    except Exception as e:
        log.error(f"Discord initial backtest notification failed: {e}")

    # 7. Start real-time websocket price feed
    collector = PriceCollector(log_interval_seconds=60)
    ws_thread = threading.Thread(
        target=collector.start,
        daemon=True,
        name="price-ws"
    )
    ws_thread.start()
    log.info("Price websocket started (BTC/ETH/SOL/XRP)")

    # 8. Schedule ongoing OHLCV collection
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        ohlcv_5min_job,
        "interval",
        minutes=5,
        id="ohlcv_5min",
        max_instances=1,
        misfire_grace_time=60
    )
    scheduler.add_job(
        ohlcv_1hour_job,
        "interval",
        hours=1,
        id="ohlcv_1hour",
        max_instances=1,
        misfire_grace_time=120
    )
    scheduler.add_job(
        daily_backtest_job,
        "cron",
        hour=6,
        minute=0,
        id="daily_backtest",
        max_instances=1,
        misfire_grace_time=3600
    )
    scheduler.start()
    log.info("OHLCV collection scheduled (5-min + 1-hour)")
    log.info("Daily backtest scheduled (06:00 UTC)")

    # 9. Start Discord listener for STOP command
    try:
        start_discord_listener()
    except Exception as e:
        log.warning(f"Discord listener not started: {e}")

    log.info("All systems running. Ctrl+C to stop.")

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
