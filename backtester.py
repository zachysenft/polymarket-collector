import logging
import pandas as pd
from datetime import datetime, timezone

from db import get_full_dataset, insert_backtest_results

log = logging.getLogger(__name__)

PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]
GRANULARITIES = ["5min", "1hour"]


def _calc_returns(trades):
    """Calculate stats from a list of (entry_price, exit_price) tuples."""
    if not trades:
        return {"trades": 0, "win_rate": 0, "avg_return": 0,
                "total_return": 0, "max_drawdown": 0}

    returns = [(exit_p - entry_p) / entry_p * 100
               for entry_p, exit_p in trades]
    wins = sum(1 for r in returns if r > 0)

    # Max drawdown from cumulative returns
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return {
        "trades":       len(trades),
        "win_rate":     round(wins / len(trades) * 100, 2),
        "avg_return":   round(sum(returns) / len(returns), 4),
        "total_return": round(sum(returns), 4),
        "max_drawdown": round(max_dd, 4),
    }


def strategy_rsi_oversold(df):
    """Buy when RSI < 30, sell when RSI > 50."""
    trades = []
    in_trade = False
    entry_price = 0

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        if pd.isna(rsi):
            continue
        if not in_trade and rsi < 30:
            entry_price = row["close"]
            in_trade = True
        elif in_trade and rsi > 50:
            trades.append((entry_price, row["close"]))
            in_trade = False

    return _calc_returns(trades)


def strategy_rsi_overbought(df):
    """Short when RSI > 70, cover when RSI < 50."""
    trades = []
    in_trade = False
    entry_price = 0

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        if pd.isna(rsi):
            continue
        if not in_trade and rsi > 70:
            entry_price = row["close"]
            in_trade = True
        elif in_trade and rsi < 50:
            # Short profit = entry - exit
            trades.append((row["close"], entry_price))
            in_trade = False

    return _calc_returns(trades)


def strategy_macd_crossover(df):
    """Buy on bullish MACD crossover, sell on bearish."""
    trades = []
    in_trade = False
    entry_price = 0
    prev_hist = None

    for _, row in df.iterrows():
        hist = row["macd_hist"]
        if pd.isna(hist):
            prev_hist = None
            continue
        if prev_hist is not None:
            if not in_trade and prev_hist <= 0 and hist > 0:
                entry_price = row["close"]
                in_trade = True
            elif in_trade and prev_hist >= 0 and hist < 0:
                trades.append((entry_price, row["close"]))
                in_trade = False
        prev_hist = hist

    return _calc_returns(trades)


def strategy_bb_bounce(df):
    """Buy when price touches lower BB, sell at middle BB."""
    trades = []
    in_trade = False
    entry_price = 0

    for _, row in df.iterrows():
        if pd.isna(row["bb_lower"]) or pd.isna(row["bb_middle"]):
            continue
        if not in_trade and row["close"] <= row["bb_lower"]:
            entry_price = row["close"]
            in_trade = True
        elif in_trade and row["close"] >= row["bb_middle"]:
            trades.append((entry_price, row["close"]))
            in_trade = False

    return _calc_returns(trades)


def strategy_bb_squeeze(df):
    """
    Enter long when BB width contracts to 20-period low then expands.
    Exit when price reaches upper band.
    """
    trades = []
    in_trade = False
    entry_price = 0

    if len(df) < 21:
        return _calc_returns(trades)

    df = df.copy()
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]
    df["bb_width_min20"] = df["bb_width"].rolling(20).min()

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        if pd.isna(row["bb_width"]) or pd.isna(row["bb_width_min20"]):
            continue

        if not in_trade:
            # Squeeze: current width near 20-period min, then expanding
            if (prev["bb_width"] <= prev["bb_width_min20"] * 1.05 and
                    row["bb_width"] > prev["bb_width"] * 1.1):
                entry_price = row["close"]
                in_trade = True
        elif row["close"] >= row["bb_upper"]:
            trades.append((entry_price, row["close"]))
            in_trade = False

    return _calc_returns(trades)


def strategy_multi_indicator(df):
    """
    High-conviction long: RSI < 35 AND price below lower BB
    AND MACD histogram turning positive.
    Exit when RSI > 55.
    """
    trades = []
    in_trade = False
    entry_price = 0
    prev_hist = None

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        hist = row["macd_hist"]
        if pd.isna(rsi) or pd.isna(hist) or pd.isna(row["bb_lower"]):
            prev_hist = hist if not pd.isna(hist) else None
            continue

        if not in_trade:
            hist_turning = (prev_hist is not None and prev_hist < 0 and hist > prev_hist)
            if rsi < 35 and row["close"] < row["bb_lower"] and hist_turning:
                entry_price = row["close"]
                in_trade = True
        elif rsi > 55:
            trades.append((entry_price, row["close"]))
            in_trade = False

        prev_hist = hist

    return _calc_returns(trades)


STRATEGIES = {
    "RSI Oversold Buy":     strategy_rsi_oversold,
    "RSI Overbought Short": strategy_rsi_overbought,
    "MACD Crossover":       strategy_macd_crossover,
    "BB Bounce":            strategy_bb_bounce,
    "BB Squeeze Breakout":  strategy_bb_squeeze,
    "Multi-Indicator Combo": strategy_multi_indicator,
}


def run_all_backtests():
    """Run all strategies across all products and granularities. Log + store results."""
    run_ts = datetime.now(timezone.utc)
    all_results = []

    log.info("=" * 70)
    log.info("  BACKTEST RESULTS")
    log.info("=" * 70)

    for gran in GRANULARITIES:
        for product in PRODUCTS:
            df = get_full_dataset(product, gran)
            if df.empty or len(df) < 30:
                log.warning(f"Skipping {product} {gran}: insufficient data ({len(df)} rows)")
                continue

            for name, func in STRATEGIES.items():
                result = func(df)
                result["run_ts"] = run_ts
                result["product"] = product
                result["granularity"] = gran
                result["strategy"] = name
                all_results.append(result)

                if result["trades"] > 0:
                    log.info(
                        f"  {product:8s} {gran:5s} | {name:25s} | "
                        f"trades={result['trades']:3d}  win={result['win_rate']:5.1f}%  "
                        f"avg={result['avg_return']:+7.3f}%  "
                        f"total={result['total_return']:+8.3f}%  "
                        f"maxDD={result['max_drawdown']:6.3f}%"
                    )
                else:
                    log.info(
                        f"  {product:8s} {gran:5s} | {name:25s} | no trades triggered"
                    )

    log.info("=" * 70)

    # Store results
    if all_results:
        insert_backtest_results(all_results)
        log.info(f"Stored {len(all_results)} backtest results to DB")

    # Print top strategies
    winners = [r for r in all_results if r["trades"] > 0]
    if winners:
        winners.sort(key=lambda x: x["total_return"], reverse=True)
        log.info("")
        log.info("  TOP 5 STRATEGIES BY TOTAL RETURN:")
        for r in winners[:5]:
            log.info(
                f"    {r['product']:8s} {r['granularity']:5s} {r['strategy']:25s} "
                f"→ {r['total_return']:+.3f}%  ({r['trades']} trades, "
                f"{r['win_rate']:.0f}% win rate)"
            )
        log.info("")

    return all_results
