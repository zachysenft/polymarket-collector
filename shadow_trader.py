import logging
import pandas as pd
from datetime import datetime, timezone

from db import (
    get_full_dataset, get_open_shadow_trades,
    get_all_strategy_balances, insert_shadow_trade, close_shadow_trade,
    update_peak_price, update_shadow_balance,
)

log = logging.getLogger(__name__)

# --- Configuration ---
# To add a new strategy: add one entry to _STRATEGY_TEMPLATES below.
# It will automatically be created for all assets, granularities, and sides.
_ASSETS = [
    ("SOL", "SOL-USD"),
    ("ETH", "ETH-USD"),
    ("BTC", "BTC-USD"),
    ("XRP", "XRP-USD"),
]
_GRANULARITIES = ["5min", "1hour", "1day"]
_STRATEGY_TEMPLATES = [
    # (label, long_entry, long_exit)
    ("MACD+RSI", "_check_entry_macd_rsi_filtered",  "_check_exit_macd_rsi_filtered"),
    ("RSI Mom",  "_check_entry_rsi_momentum",        "_check_exit_rsi_momentum"),
    ("MACD Cross","_check_entry_macd_crossover",     "_check_exit_macd_crossover"),
]

def _build_strategies():
    strategies = {}
    for label, entry, exit_ in _STRATEGY_TEMPLATES:
        short_entry = entry + "_short"
        short_exit  = exit_ + "_short"
        for asset_label, product in _ASSETS:
            for gran in _GRANULARITIES:
                base = f"{asset_label} {label}"
                strategies[f"{base} Long {gran}"]  = {"product": product, "granularity": gran, "entry": entry,       "exit": exit_,       "side": "long"}
                strategies[f"{base} Short {gran}"] = {"product": product, "granularity": gran, "entry": short_entry, "exit": short_exit,  "side": "short"}
                strategies[f"{base} Combo {gran}"] = {"product": product, "granularity": gran, "entry": entry,       "exit": exit_,       "entry_short": short_entry, "exit_short": short_exit, "side": "both"}
    return strategies

SHADOW_STRATEGIES = _build_strategies()

# Wide params — best performer from param sweep
SHADOW_RISK_PARAMS = {"sl_pct": 0.04, "tp_pct": 0.06, "trail_pct": 0.04}

INITIAL_BALANCE = 100.0
POSITION_SIZE_PCT = 0.10   # 10% per trade of each strategy's own balance
MAX_EXPOSURE_PCT = 0.50    # 50% cap per strategy
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


def _check_entry_macd_rsi_filtered_short(prev, curr):
    """MACD hist crosses negative AND RSI between 40-70 (room to fall)."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    rsi = curr.get("rsi_14")
    if pd.isna(prev_hist) or pd.isna(curr_hist) or pd.isna(rsi):
        return False
    return prev_hist >= 0 and curr_hist < 0 and 40 <= rsi <= 70


def _check_exit_macd_rsi_filtered_short(prev, curr):
    """MACD hist crosses positive."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist <= 0 and curr_hist > 0


def _check_entry_rsi_momentum_short(prev, curr):
    """RSI crosses below 50 from above."""
    prev_rsi = prev.get("rsi_14")
    curr_rsi = curr.get("rsi_14")
    if pd.isna(prev_rsi) or pd.isna(curr_rsi):
        return False
    return prev_rsi > 50 and curr_rsi <= 50


def _check_exit_rsi_momentum_short(prev, curr):
    """RSI rises above 55."""
    rsi = curr.get("rsi_14")
    if pd.isna(rsi):
        return False
    return rsi > 55


def _check_entry_macd_crossover_short(prev, curr):
    """MACD hist crosses negative."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist >= 0 and curr_hist < 0


def _check_exit_macd_crossover_short(prev, curr):
    """MACD hist crosses positive."""
    prev_hist = prev.get("macd_hist")
    curr_hist = curr.get("macd_hist")
    if pd.isna(prev_hist) or pd.isna(curr_hist):
        return False
    return prev_hist <= 0 and curr_hist > 0


ENTRY_FUNCS = {
    "_check_entry_macd_rsi_filtered": _check_entry_macd_rsi_filtered,
    "_check_entry_rsi_momentum": _check_entry_rsi_momentum,
    "_check_entry_macd_crossover": _check_entry_macd_crossover,
    "_check_entry_macd_rsi_filtered_short": _check_entry_macd_rsi_filtered_short,
    "_check_entry_rsi_momentum_short": _check_entry_rsi_momentum_short,
    "_check_entry_macd_crossover_short": _check_entry_macd_crossover_short,
}

EXIT_FUNCS = {
    "_check_exit_macd_rsi_filtered": _check_exit_macd_rsi_filtered,
    "_check_exit_rsi_momentum": _check_exit_rsi_momentum,
    "_check_exit_macd_crossover": _check_exit_macd_crossover,
    "_check_exit_macd_rsi_filtered_short": _check_exit_macd_rsi_filtered_short,
    "_check_exit_rsi_momentum_short": _check_exit_rsi_momentum_short,
    "_check_exit_macd_crossover_short": _check_exit_macd_crossover_short,
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

def evaluate_shadow_trades(granularity):
    """Check exits then entries for all strategies matching the given granularity.
    Called after each candle collection job (5min, 1hour, 1day).
    Each strategy manages its own $100 balance independently.
    """
    active_strategies = {k: v for k, v in SHADOW_STRATEGIES.items() if v["granularity"] == granularity}
    active_strategy_names = set(active_strategies.keys())

    # Cache DataFrames per product to avoid redundant DB queries
    df_cache = {}

    def _get_df(product):
        if product not in df_cache:
            df_cache[product] = get_full_dataset(product, granularity)
        return df_cache[product]

    # Pre-fetch open positions and strategy balances once
    all_open = get_open_shadow_trades()
    open_positions = [p for p in all_open if p["strategy"] in active_strategy_names]
    all_balances = get_all_strategy_balances()
    closed_ids = set()

    # --- PHASE 1: Check exits on open positions (this granularity only) ---
    for pos in open_positions:
        product = pos["product"]
        strat_name = pos["strategy"]
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
            balance = all_balances.get(strat_name, INITIAL_BALANCE)
            new_balance = round(balance + pnl_dollars, 2)
            update_shadow_balance(new_balance, "trade_close", strategy=strat_name)
            all_balances[strat_name] = new_balance
            closed_ids.add(pos["id"])
            log.info(f"SHADOW EXIT [{strat_name}] {product} {reason} "
                     f"@ {exit_price:.4f} P&L: ${pnl_dollars:+.2f} ({pnl_pct*100:+.2f}%)")
            continue

        # 1b. Update peak price
        if new_peak != float(pos["peak_price"]):
            update_peak_price(pos["id"], new_peak)

        # 1c. Signal-based exit (combined strategies use side-appropriate exit function)
        strat_config = SHADOW_STRATEGIES.get(strat_name)
        if strat_config:
            exit_key = strat_config.get("exit_short", strat_config["exit"]) if pos["side"] == "short" else strat_config["exit"]
            exit_func = EXIT_FUNCS.get(exit_key)
            if exit_func and exit_func(prev, curr):
                exit_price = float(curr["close"])
                pnl_pct = (exit_price - float(pos["entry_price"])) / float(pos["entry_price"]) - ROUND_TRIP_FEE
                if pos["side"] == "short":
                    pnl_pct = (float(pos["entry_price"]) - exit_price) / float(pos["entry_price"]) - ROUND_TRIP_FEE
                pnl_dollars = float(pos["position_size"]) * pnl_pct
                close_shadow_trade(pos["id"], exit_price, curr["ts"], "signal",
                                   round(pnl_dollars, 4), round(pnl_pct * 100, 4))
                balance = all_balances.get(strat_name, INITIAL_BALANCE)
                new_balance = round(balance + pnl_dollars, 2)
                update_shadow_balance(new_balance, "trade_close", strategy=strat_name)
                all_balances[strat_name] = new_balance
                closed_ids.add(pos["id"])
                log.info(f"SHADOW EXIT [{strat_name}] {product} signal "
                         f"@ {exit_price:.4f} P&L: ${pnl_dollars:+.2f} ({pnl_pct*100:+.2f}%)")

    # --- PHASE 2: Check for new entry signals (per-strategy balance) ---
    # Positions still open after Phase 1 exits
    still_open = [p for p in open_positions if p["id"] not in closed_ids]

    for strat_name, config in active_strategies.items():
        product = config["product"]
        df = _get_df(product)
        if len(df) < 2:
            continue

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Build list of (side, entry_func) signals to check
        signals_to_check = [("long", ENTRY_FUNCS.get(config["entry"]))]
        if config["side"] == "both" and config.get("entry_short"):
            signals_to_check.append(("short", ENTRY_FUNCS.get(config["entry_short"])))

        for trade_side, entry_func in signals_to_check:
            if not entry_func or not entry_func(prev, curr):
                continue

            # Duplicate prevention: don't enter same strategy+side on same candle
            strat_open = [p for p in still_open if p["strategy"] == strat_name]
            if any(str(p["entry_ts"]) == str(curr["ts"]) and p["side"] == trade_side for p in strat_open):
                continue

            # Per-strategy balance and exposure
            balance = all_balances.get(strat_name, INITIAL_BALANCE)
            if balance <= 0:
                log.warning(f"Shadow balance <= 0 for {strat_name}, skipping entry")
                continue

            position_size = round(balance * POSITION_SIZE_PCT, 2)
            strat_exposure = sum(float(p["position_size"]) for p in strat_open)

            if strat_exposure + position_size > balance * MAX_EXPOSURE_PCT:
                insert_shadow_trade({
                    "strategy": strat_name, "product": product, "side": trade_side,
                    "status": "skipped", "entry_ts": curr["ts"],
                    "entry_price": float(curr["close"]), "position_size": 0,
                    "peak_price": 0, "sl_pct": 0, "tp_pct": 0, "trail_pct": 0,
                    "notes": f"skipped: exposure cap (${strat_exposure:.2f}/{balance * MAX_EXPOSURE_PCT:.2f})",
                })
                log.info(f"SHADOW SKIP [{strat_name}] {product} {trade_side} — exposure cap "
                         f"(${strat_exposure:.2f}/{balance * MAX_EXPOSURE_PCT:.2f})")
                continue

            entry_price = float(curr["close"])
            peak_price = float(curr["high"]) if trade_side == "long" else float(curr["low"])
            insert_shadow_trade({
                "strategy": strat_name,
                "product": product,
                "side": trade_side,
                "status": "open",
                "entry_ts": curr["ts"],
                "entry_price": entry_price,
                "position_size": position_size,
                "peak_price": peak_price,
                "sl_pct": SHADOW_RISK_PARAMS["sl_pct"],
                "tp_pct": SHADOW_RISK_PARAMS["tp_pct"],
                "trail_pct": SHADOW_RISK_PARAMS["trail_pct"],
                "entry_rsi": float(curr["rsi_14"]) if not pd.isna(curr.get("rsi_14")) else None,
                "entry_macd_hist": float(curr["macd_hist"]) if not pd.isna(curr.get("macd_hist")) else None,
                "entry_adx": float(curr["adx_14"]) if not pd.isna(curr.get("adx_14")) else None,
                "entry_atr": float(curr["atr_14"]) if not pd.isna(curr.get("atr_14")) else None,
            })
            sl_price = entry_price * (1 - SHADOW_RISK_PARAMS["sl_pct"]) if trade_side == "long" else entry_price * (1 + SHADOW_RISK_PARAMS["sl_pct"])
            tp_price = entry_price * (1 + SHADOW_RISK_PARAMS["tp_pct"]) if trade_side == "long" else entry_price * (1 - SHADOW_RISK_PARAMS["tp_pct"])
            log.info(f"SHADOW ENTRY [{strat_name}] {product} {trade_side} @ {entry_price:.4f} "
                     f"size=${position_size:.2f} bal=${balance:.2f} "
                     f"SL={sl_price:.4f} TP={tp_price:.4f}")

    log.info(f"Shadow eval [{granularity}] complete — {len(still_open)} open positions across {len(active_strategies)} strategies")
