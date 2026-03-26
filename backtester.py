import logging
import pandas as pd
from datetime import datetime, timezone

from db import get_full_dataset, insert_backtest_results

log = logging.getLogger(__name__)

PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]
GRANULARITIES = ["5min", "1hour"]

# Risk management per asset (based on volatility research)
RISK_PARAMS = {
    "BTC-USD": {"sl_pct": 0.03, "tp_pct": 0.06, "trail_pct": 0.05},
    "ETH-USD": {"sl_pct": 0.04, "tp_pct": 0.08, "trail_pct": 0.05},
    "SOL-USD": {"sl_pct": 0.05, "tp_pct": 0.10, "trail_pct": 0.06},
    "XRP-USD": {"sl_pct": 0.05, "tp_pct": 0.10, "trail_pct": 0.06},
}

# Binance.US Tier 0 fees: 0.00% maker + 0.01% taker on BTC/ETH/SOL/USD pairs
ROUND_TRIP_FEE = 0.0002  # 0.02% total (limit entry + market exit worst case)


def _calc_returns(trades):
    """Calculate stats from a list of (entry_price, exit_price, exit_reason) tuples."""
    if not trades:
        return {"trades": 0, "win_rate": 0, "avg_return": 0,
                "total_return": 0, "max_drawdown": 0,
                "sl_exits": 0, "tp_exits": 0, "trail_exits": 0, "signal_exits": 0}

    returns = [(exit_p - entry_p) / entry_p - ROUND_TRIP_FEE
               for entry_p, exit_p, _ in trades]
    # Convert to percentages
    returns_pct = [r * 100 for r in returns]
    wins = sum(1 for r in returns_pct if r > 0)

    # Exit reason counts
    reasons = [t[2] for t in trades]
    sl_exits = reasons.count("sl")
    tp_exits = reasons.count("tp")
    trail_exits = reasons.count("trail")
    signal_exits = reasons.count("signal")

    # Max drawdown from cumulative returns
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in returns_pct:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return {
        "trades":       len(trades),
        "win_rate":     round(wins / len(trades) * 100, 2),
        "avg_return":   round(sum(returns_pct) / len(returns_pct), 4),
        "total_return": round(sum(returns_pct), 4),
        "max_drawdown": round(max_dd, 4),
        "sl_exits":     sl_exits,
        "tp_exits":     tp_exits,
        "trail_exits":  trail_exits,
        "signal_exits": signal_exits,
    }


def _check_risk_exit(row, entry_price, peak_price, product, is_short=False):
    """
    Check if SL, TP, or trailing stop should trigger.
    Returns (should_exit, exit_reason) or (False, None).
    """
    params = RISK_PARAMS[product]
    price = row["close"]

    if is_short:
        # Short: SL if price rises too much, TP if price drops enough
        pct_move = (price - entry_price) / entry_price
        if pct_move >= params["sl_pct"]:
            return True, "sl"
        if pct_move <= -params["tp_pct"]:
            return True, "tp"
        # Trailing: track lowest price, exit if bounces up
        pct_from_trough = (price - peak_price) / peak_price if peak_price > 0 else 0
        if pct_from_trough >= params["trail_pct"]:
            return True, "trail"
    else:
        # Long: SL if price drops, TP if price rises
        pct_move = (price - entry_price) / entry_price
        if pct_move <= -params["sl_pct"]:
            return True, "sl"
        if pct_move >= params["tp_pct"]:
            return True, "tp"
        # Trailing: track highest price, exit if drops from peak
        pct_from_peak = (peak_price - price) / peak_price if peak_price > 0 else 0
        if pct_from_peak >= params["trail_pct"] and pct_move > 0:
            return True, "trail"

    return False, None


def strategy_rsi_oversold(df, product):
    """Buy when RSI < 30, sell when RSI > 50 (or SL/TP/trail)."""
    trades = []
    in_trade = False
    entry_price = 0
    peak_price = 0

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        if pd.isna(rsi):
            continue

        if in_trade:
            peak_price = max(peak_price, row["close"])
            should_exit, reason = _check_risk_exit(row, entry_price, peak_price, product)
            if should_exit:
                trades.append((entry_price, row["close"], reason))
                in_trade = False
                continue
            if rsi > 50:
                trades.append((entry_price, row["close"], "signal"))
                in_trade = False
        elif rsi < 30:
            entry_price = row["close"]
            peak_price = row["close"]
            in_trade = True

    return _calc_returns(trades)


def strategy_rsi_overbought(df, product):
    """Short when RSI > 70, cover when RSI < 50 (or SL/TP/trail)."""
    trades = []
    in_trade = False
    entry_price = 0
    trough_price = 0

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        if pd.isna(rsi):
            continue

        if in_trade:
            trough_price = min(trough_price, row["close"])
            should_exit, reason = _check_risk_exit(
                row, entry_price, trough_price, product, is_short=True)
            if should_exit:
                # Short: profit = entry - exit
                trades.append((row["close"], entry_price, reason))
                in_trade = False
                continue
            if rsi < 50:
                trades.append((row["close"], entry_price, "signal"))
                in_trade = False
        elif rsi > 70:
            entry_price = row["close"]
            trough_price = row["close"]
            in_trade = True

    return _calc_returns(trades)


def strategy_macd_crossover(df, product):
    """Buy on bullish MACD crossover, sell on bearish (or SL/TP/trail)."""
    trades = []
    in_trade = False
    entry_price = 0
    peak_price = 0
    prev_hist = None

    for _, row in df.iterrows():
        hist = row["macd_hist"]
        if pd.isna(hist):
            prev_hist = None
            continue

        if in_trade:
            peak_price = max(peak_price, row["close"])
            should_exit, reason = _check_risk_exit(row, entry_price, peak_price, product)
            if should_exit:
                trades.append((entry_price, row["close"], reason))
                in_trade = False
                prev_hist = hist
                continue
            if prev_hist is not None and prev_hist >= 0 and hist < 0:
                trades.append((entry_price, row["close"], "signal"))
                in_trade = False

        elif prev_hist is not None and prev_hist <= 0 and hist > 0:
            entry_price = row["close"]
            peak_price = row["close"]
            in_trade = True

        prev_hist = hist

    return _calc_returns(trades)


def strategy_bb_bounce(df, product):
    """Buy when price touches lower BB, sell at middle BB (or SL/TP/trail)."""
    trades = []
    in_trade = False
    entry_price = 0
    peak_price = 0

    for _, row in df.iterrows():
        if pd.isna(row["bb_lower"]) or pd.isna(row["bb_middle"]):
            continue

        if in_trade:
            peak_price = max(peak_price, row["close"])
            should_exit, reason = _check_risk_exit(row, entry_price, peak_price, product)
            if should_exit:
                trades.append((entry_price, row["close"], reason))
                in_trade = False
                continue
            if row["close"] >= row["bb_middle"]:
                trades.append((entry_price, row["close"], "signal"))
                in_trade = False
        elif row["close"] <= row["bb_lower"]:
            entry_price = row["close"]
            peak_price = row["close"]
            in_trade = True

    return _calc_returns(trades)


def strategy_bb_squeeze(df, product):
    """
    Enter long when BB width contracts to 20-period low then expands.
    Exit when price reaches upper band (or SL/TP/trail).
    """
    trades = []
    in_trade = False
    entry_price = 0
    peak_price = 0

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

        if in_trade:
            peak_price = max(peak_price, row["close"])
            should_exit, reason = _check_risk_exit(row, entry_price, peak_price, product)
            if should_exit:
                trades.append((entry_price, row["close"], reason))
                in_trade = False
                continue
            if row["close"] >= row["bb_upper"]:
                trades.append((entry_price, row["close"], "signal"))
                in_trade = False
        else:
            if (prev["bb_width"] <= prev["bb_width_min20"] * 1.05 and
                    row["bb_width"] > prev["bb_width"] * 1.1):
                entry_price = row["close"]
                peak_price = row["close"]
                in_trade = True

    return _calc_returns(trades)


def strategy_multi_indicator(df, product):
    """
    High-conviction long: RSI < 35 AND price below lower BB
    AND MACD histogram turning positive.
    Exit when RSI > 55 (or SL/TP/trail).
    """
    trades = []
    in_trade = False
    entry_price = 0
    peak_price = 0
    prev_hist = None

    for _, row in df.iterrows():
        rsi = row["rsi_14"]
        hist = row["macd_hist"]
        if pd.isna(rsi) or pd.isna(hist) or pd.isna(row["bb_lower"]):
            prev_hist = hist if not pd.isna(hist) else None
            continue

        if in_trade:
            peak_price = max(peak_price, row["close"])
            should_exit, reason = _check_risk_exit(row, entry_price, peak_price, product)
            if should_exit:
                trades.append((entry_price, row["close"], reason))
                in_trade = False
                prev_hist = hist
                continue
            if rsi > 55:
                trades.append((entry_price, row["close"], "signal"))
                in_trade = False
        else:
            hist_turning = (prev_hist is not None and prev_hist < 0 and hist > prev_hist)
            if rsi < 35 and row["close"] < row["bb_lower"] and hist_turning:
                entry_price = row["close"]
                peak_price = row["close"]
                in_trade = True

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
    log.info("  BACKTEST RESULTS (with SL/TP/trailing stop, fees=0.02%%)")
    log.info("  SL: BTC 3%%, ETH 4%%, SOL/XRP 5%%  |  TP: 2:1 ratio")
    log.info("  Trailing stop: 5-6%% from peak  |  Fees: 0.02%% round trip (Binance.US)")
    log.info("=" * 70)

    for gran in GRANULARITIES:
        for product in PRODUCTS:
            df = get_full_dataset(product, gran)
            if df.empty or len(df) < 30:
                log.warning(f"Skipping {product} {gran}: insufficient data ({len(df)} rows)")
                continue

            for name, func in STRATEGIES.items():
                result = func(df, product)
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
                        f"maxDD={result['max_drawdown']:6.3f}%  "
                        f"exits: SL={result['sl_exits']} TP={result['tp_exits']} "
                        f"trail={result['trail_exits']} sig={result['signal_exits']}"
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
    winners = [r for r in all_results if r["trades"] > 0 and r["total_return"] > 0]
    if winners:
        winners.sort(key=lambda x: x["total_return"], reverse=True)
        log.info("")
        log.info("  TOP PROFITABLE STRATEGIES (after fees + risk management):")
        for r in winners[:5]:
            log.info(
                f"    {r['product']:8s} {r['granularity']:5s} {r['strategy']:25s} "
                f"→ {r['total_return']:+.3f}%  ({r['trades']} trades, "
                f"{r['win_rate']:.0f}% win rate, maxDD={r['max_drawdown']:.2f}%)"
            )
    else:
        log.info("")
        log.info("  No strategies were profitable after fees + risk management.")
        log.info("  This is normal — most naive strategies don't survive realistic costs.")
    log.info("")

    return all_results
