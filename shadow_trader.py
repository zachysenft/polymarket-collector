import logging
import pandas as pd
from datetime import datetime, timezone

from db import (
    get_full_dataset, get_open_shadow_trades, get_shadow_balance,
    insert_shadow_trade, close_shadow_trade, update_peak_price,
    update_shadow_balance,
)

log = logging.getLogger(__name__)

# --- Configuration ---
SHADOW_STRATEGIES = {
    "SOL MACD+RSI": {"product": "SOL-USD", "entry": "_check_entry_macd_rsi_filtered",
                      "exit": "_check_exit_macd_rsi_filtered", "side": "long"},
    "SOL RSI Mom":  {"product": "SOL-USD", "entry": "_check_entry_rsi_momentum",
                      "exit": "_check_exit_rsi_momentum", "side": "long"},
    "ETH MACD Cross": {"product": "ETH-USD", "entry": "_check_entry_macd_crossover",
                        "exit": "_check_exit_macd_crossover", "side": "long"},
}

# Wide params — best performer from param sweep
SHADOW_RISK_PARAMS = {"sl_pct": 0.04, "tp_pct": 0.06, "trail_pct": 0.04}

INITIAL_BALANCE = 100.0
POSITION_SIZE_PCT = 0.05   # 5% per trade
MAX_EXPOSURE_PCT = 0.50    # 50% cap
ROUND_TRIP_FEE = 0.0002    # 0.02% matching backtester


# --- Signal Detection (extracted from backtester.py strategy logic) ---

def _check_entry_macd_rsi_filtered(prev, curr):
    """MACD hist crosses positive AND RSI between 30-60."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    rsi = curr.get("rsi_14")
    if pd.isna(prev_hist) or pd.isna(curr_hist) or pd.isna(rsi):
        return False
    return prev_hist <= 0 and curr_hist > 0 and 30 <= rsi <= 60


def _check_exit_macd_rsi_filtered(prev, curr):
    """MACD hist crosses negative."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist >= 0 and curr_hist < 0


def _check_entry_rsi_momentum(prev, curr):
    """RSI crosses above 50 from below."""
    prev_rsi = prev.get("rsi_14")
    curr_rsi = curr.get("rsi_14")
    if pd.isna(prev_rsi) or pd.isna(curr_rsi):
        return False
    return prev_rsi < 50 and curr_rsi >= 50


def _check_exit_rsi_momentum(prev, curr):
    """RSI drops below 45."""
    rsi = curr.get("rsi_14")
    if pd.isna(rsi):
        return False
    return rsi < 45


def _check_entry_macd_crossover(prev, curr):
    """MACD hist crosses positive."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist <= 0 and curr_hist > 0


def _check_exit_macd_crossover(prev, curr):
    """MACD hist crosses negative."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist >= 0 and curr_hist < 0


ENTRY_FUNCS = {
    "_check_entry_macd_rsi_filtered": _check_entry_macd_rsi_filtered,
    "_check_entry_rsi_momentum": _check_entry_rsi_momentum,
    "_check_entry_macd_crossover": _check_entry_macd_crossover,
}

EXIT_FUNCS = {
    "_check_exit_macd_rsi_filtered": _check_exit_macd_rsi_filtered,
    "_check_exit_rsi_momentum": _check_exit_rsi_momentum,
    "_check_exit_macd_crossover": _check_exit_macd_crossover,
}


# --- Intra-Candle Risk Exit ---

def _check_intra_candle_risk(pos, candle):
    """
    Check SL/TP/trail using candle high/low for realistic detection.
    Returns (should_exit, reason, exit_price, new_peak).
    """
    entry_price = float(pos["entry_price"])
    peak_price = float(pos["peak_price"])
    sl_pct = float(pos["sl_pct"])
    tp_pct = float(pos["tp_pct"])
    trail_pct = float(pos["trail_pct"])

    candle_high = float(candle["high"])
    candle_low = float(candle["low"])
    candle_close = float(candle["close"])

    if pos["side"] == "long":
        new_peak = max(peak_price, candle_high)

        # SL: did price drop to SL level?
        sl_level = entry_price * (1 - sl_pct)
        if candle_low <= sl_level:
            return True, "sl", sl_level, new_peak

        # TP: did price reach TP level?
        tp_level = entry_price * (1 + tp_pct)
        if candle_high >= tp_level:
            return True, "tp", tp_level, new_peak

        # Trail: did price drop from peak?
        trail_level = new_peak * (1 - trail_pct)
        pct_move = (candle_close - entry_price) / entry_price
        if candle_low <= trail_level and pct_move > 0:
            return True, "trail", trail_level, new_peak

        return False, None, None, new_peak
    else:
        # Short
        new_trough = min(peak_price, candle_low)

        sl_level = entry_price * (1 + sl_pct)
        if candle_high >= sl_level:
            return True, "sl", sl_level, new_trough

        tp_level = entry_price * (1 - tp_pct)
        if candle_low <= tp_level:
            return True, "tp", tp_level, new_trough

        trail_level = new_trough * (1 + trail_pct)
        pct_move = (entry_price - candle_close) / entry_price
        if candle_high >= trail_level and pct_move > 0:
            return True, "trail", trail_level, new_trough

        return False, None, None, new_trough


# --- Main Evaluation ---

def evaluate_shadow_trades():
    """Called after each 1-hour candle collection. Checks exits then entries."""

    # Cache DataFrames per product to avoid redundant DB queries
    df_cache = {}

    def _get_df(product):
        if product not in df_cache:
            df_cache[product] = get_full_dataset(product, "1hour")
        return df_cache[product]

    # --- PHASE 1: Check exits on open positions ---
    open_positions = get_open_shadow_trades()
    for pos in open_positions:
        product = pos["product"]
        df = _get_df(product)
        if len(df) < 2:
            continue

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # 1a. Intra-candle SL/TP/trail check
        should_exit, reason, exit_price, new_peak = _check_intra_candle_risk(pos, curr)

        if should_exit:
            pnl_pct = (exit_price - float(pos["entry_price"])) / float(pos["entry_price"]) - ROUND_TRIP_FEE
            if pos["side"] == "short":
                pnl_pct = (float(pos["entry_price"]) - exit_price) / float(pos["entry_price"]) - ROUND_TRIP_FEE
            pnl_dollars = float(pos["position_size"]) * pnl_pct
            close_shadow_trade(pos["id"], exit_price, curr["ts"], reason,
                               round(pnl_dollars, 4), round(pnl_pct * 100, 4))
            # Update balance
            balance = get_shadow_balance()
            update_shadow_balance(round(balance + pnl_dollars, 2), "trade_close")
            log.info(f"SHADOW EXIT [{pos['strategy']}] {pos['product']} {reason} "
                     f"@ {exit_price:.4f} P&L: ${pnl_dollars:+.2f} ({pnl_pct*100:+.2f}%)")
            continue

        # 1b. Update peak price
        if new_peak != float(pos["peak_price"]):
            update_peak_price(pos["id"], new_peak)

        # 1c. Signal-based exit
        strat_config = SHADOW_STRATEGIES.get(pos["strategy"])
        if strat_config:
            exit_func = EXIT_FUNCS.get(strat_config["exit"])
            if exit_func and exit_func(prev, curr):
                exit_price = float(curr["close"])
                pnl_pct = (exit_price - float(pos["entry_price"])) / float(pos["entry_price"]) - ROUND_TRIP_FEE
                if pos["side"] == "short":
                    pnl_pct = (float(pos["entry_price"]) - exit_price) / float(pos["entry_price"]) - ROUND_TRIP_FEE
                pnl_dollars = float(pos["position_size"]) * pnl_pct
                close_shadow_trade(pos["id"], exit_price, curr["ts"], "signal",
                                   round(pnl_dollars, 4), round(pnl_pct * 100, 4))
                balance = get_shadow_balance()
                update_shadow_balance(round(balance + pnl_dollars, 2), "trade_close")
                log.info(f"SHADOW EXIT [{pos['strategy']}] {pos['product']} signal "
                         f"@ {exit_price:.4f} P&L: ${pnl_dollars:+.2f} ({pnl_pct*100:+.2f}%)")

    # --- PHASE 2: Check for new entry signals ---
    balance = get_shadow_balance()
    if balance <= 0:
        log.warning("Shadow balance <= 0, skipping entries")
        return

    # Calculate current total exposure
    open_positions = get_open_shadow_trades()  # refresh after exits
    total_exposure = sum(float(p["position_size"]) for p in open_positions)
    position_size = round(balance * POSITION_SIZE_PCT, 2)

    for strat_name, config in SHADOW_STRATEGIES.items():
        product = config["product"]
        df = _get_df(product)
        if len(df) < 2:
            continue

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Check entry signal
        entry_func = ENTRY_FUNCS.get(config["entry"])
        if not entry_func or not entry_func(prev, curr):
            continue

        # Duplicate prevention: don't enter same strategy on same candle
        existing = get_open_shadow_trades(strategy=strat_name)
        if any(str(p["entry_ts"]) == str(curr["ts"]) for p in existing):
            continue

        # Exposure cap check
        if total_exposure + position_size > balance * MAX_EXPOSURE_PCT:
            insert_shadow_trade({
                "strategy": strat_name, "product": product, "side": config["side"],
                "status": "skipped", "entry_ts": curr["ts"],
                "entry_price": float(curr["close"]), "position_size": 0,
                "peak_price": 0, "sl_pct": 0, "tp_pct": 0, "trail_pct": 0,
                "notes": f"skipped: exposure cap (${total_exposure:.2f}/{balance * MAX_EXPOSURE_PCT:.2f})",
            })
            log.info(f"SHADOW SKIP [{strat_name}] {product} — exposure cap "
                     f"(${total_exposure:.2f}/{balance * MAX_EXPOSURE_PCT:.2f})")
            continue

        # Open position
        entry_price = float(curr["close"])
        trade_id = insert_shadow_trade({
            "strategy": strat_name,
            "product": product,
            "side": config["side"],
            "status": "open",
            "entry_ts": curr["ts"],
            "entry_price": entry_price,
            "position_size": position_size,
            "peak_price": float(curr["high"]),
            "sl_pct": SHADOW_RISK_PARAMS["sl_pct"],
            "tp_pct": SHADOW_RISK_PARAMS["tp_pct"],
            "trail_pct": SHADOW_RISK_PARAMS["trail_pct"],
            "entry_rsi": float(curr["rsi_14"]) if not pd.isna(curr.get("rsi_14")) else None,
            "entry_macd_hist": float(curr["macd_hist"]) if not pd.isna(curr.get("macd_hist")) else None,
            "entry_adx": float(curr["adx_14"]) if not pd.isna(curr.get("adx_14")) else None,
            "entry_atr": float(curr["atr_14"]) if not pd.isna(curr.get("atr_14")) else None,
        })
        total_exposure += position_size
        log.info(f"SHADOW ENTRY [{strat_name}] {product} long @ {entry_price:.4f} "
                 f"size=${position_size:.2f} SL={entry_price*(1-SHADOW_RISK_PARAMS['sl_pct']):.4f} "
                 f"TP={entry_price*(1+SHADOW_RISK_PARAMS['tp_pct']):.4f}")

    log.info(f"Shadow eval complete — {len(open_positions)} open positions, balance=${balance:.2f}")
