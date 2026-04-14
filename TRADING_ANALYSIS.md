# Shadow Trading Analysis — Running Notes

Live shadow trading started 2026-03-27. Each entry below is dated and captures key findings as data accumulates.

---

## Day 17 Analysis (2026-04-14)

### Data snapshot
- Portfolio: $8,715.92 / $8,700 started (+$15.92 net)
- All-time realized P&L: +$16.70 (before direction bug fix — see bugs below)
- 2,839W / 3,669L (43.6% win rate across all strategies)
- 87 active strategies across 4 assets × 3 timeframes × 3 variants (Long/Short/Combo)

---

### Performance by Timeframe

| Timeframe | Trades | Avg hold | Total P&L | P&L/trade |
|-----------|--------|----------|-----------|-----------|
| 5min      | 6,043  | 1.79h    | +$3.01    | $0.0005   |
| 1hour     | 469    | 21.64h   | +$12.60   | $0.027    |
| 1day      | 12     | 114h     | +$3.77    | $0.314    |

**Key finding:** 1hour has 54× more edge per trade than 5min. 1day shows $0.314/trade but only 12 trades — too small to trust yet.

5min positions average 1.79 hours hold despite the timeframe name — signals fire on 5min candles but exits take ~22 candles. This means 5min is generating noise-level returns per trade while consuming most of the trade count. Consider reducing 5min position sizing or cutting entirely at day 30 review.

---

### Performance by Period — Regime Sensitivity

| Period    | Wins  | Losses | Total P&L | Win rate | Avg/trade |
|-----------|-------|--------|-----------|----------|-----------|
| Days 1–9  | 2,003 | 2,901  | −$3.93    | 40.8%    | −$0.0008  |
| Days 10–17| 841   | 775    | +$23.30   | 52.0%    | +$0.0144  |

Days 1–9 includes the April 2 tariff crash. Days 10–17 = recovery rally. The system is heavily long-biased — it bleeds in downtrends and wins in uptrends. The Short direction bug (see below) made downtrend performance worse: Short strategies were accidentally going LONG on bearish signals.

**Risk:** Without working short coverage, another crash = another −$4+ drawdown in 9 days.

---

### Top Strategies (17-day realized P&L, sorted by total)

| Strategy | Trades | Win% | Total P&L | $/trade |
|----------|--------|------|-----------|---------|
| ETH MACD Cross Long 1h | 16 | 75% | +$3.69 | +$0.231 |
| ETH MACD+RSI Long 1h | 12 | 75% | +$2.98 | +$0.249 |
| SOL MACD Cross Long 5m | 161 | 42% | +$1.63 | +$0.010 |
| ETH MACD Cross Long 5m | 156 | 44% | +$1.53 | +$0.010 |
| BTC RSI Mom Long 1h | 17 | 53% | +$1.43 | +$0.084 |
| BTC MACD+RSI Long 1h | 5 | 80% | +$0.98 | +$0.195 |
| BTC MACD Cross Long 1h | 7 | 57% | +$0.87 | +$0.125 |
| BTC MACD+RSI Long 1d | 2 | 100% | +$1.20 | +$0.598 |
| BTC MACD Cross Long 1d | 2 | 100% | +$1.20 | +$0.598 |

ETH MACD strategies on 1h are the core of this system. $0.23–0.25 per trade is exceptional for a $10 position (2.3–2.5% per trade average). Backtest also confirms ETH 1h MACD Cross at +20.92%.

---

### Strategies to Cut

Clear enough sample, clear negative edge:

| Strategy | Trades | Win% | Total P&L | Reason |
|----------|--------|------|-----------|--------|
| XRP RSI Mom Long 1h | 16 | 6% | −$0.90 | 1W/15L — broken signal for XRP 1h |
| SOL RSI Mom Long 1h | 19 | 26% | −$1.20 | 5W/14L — RSI momentum not working |
| ETH RSI Mom Long 5m | 152 | 18% | −$0.90 | 28W/124L — too many whipsaw losses |

RSI Momentum Long on 1h works well for BTC (+$0.084/trade) and ETH (+$0.025/trade) but fails for SOL and XRP in this period. Likely a regime/trend issue rather than a permanent signal failure — consider re-enabling if market structure changes.

---

### Bugs Found and Fixed (day 17)

#### Bug 1: Short strategies were entering LONG positions
**Root cause:** Phase 2 entry loop hardcoded `"long"` as trade side regardless of `config["side"]`:
```python
# Before (broken):
signals_to_check = [("long", ENTRY_FUNCS.get(config["entry"]))]

# After (fixed):
signals_to_check = [(config["side"], ENTRY_FUNCS.get(config["entry"]))]
```

**Impact:** Every Short variant trade from day 1 to day 17 is actually a LONG position opened on a bearish signal. All Short strategy P&L data before 2026-04-14 is invalid — it represents accidental contrarian-long behavior, not actual shorts. Short strategy analysis should filter to `entry_ts > '2026-04-14'`.

**Interesting artifact:** "Short" RSI Momentum 1h strategies accidentally discovered that "buy when RSI drops below 50" is a decent mean-reversion dip-buy signal (SOL 84% win rate, XRP 78%, BTC 86%). This is worth exploring as an intentional strategy.

#### Bug 2: Zombie positions from day 1 (legacy naming)
**Root cause:** Original strategy names had no "Long"/"Short" suffix (e.g. `ETH MACD Cross 5min`). After renaming to `ETH MACD Cross Long 5min`, `SHADOW_STRATEGIES.get()` returns None, skipping the signal exit check. Positions only exit via SL/TP/trail.

**Affected positions (still open as of day 17):**
- ETH MACD Cross 5min: entry $1,980.57 → ~+$2.00 unrealized
- ETH MACD+RSI 5min: entry $1,980.57 → ~+$2.00 unrealized
- BTC MACD+RSI 5min: entry $66,136 → ~+$1.26 unrealized
- SOL RSI Mom 5min: entry $83.11
- XRP RSI Mom 5min: entry $1.334

All are profitable. Will close when trailing stop triggers (4% below peak). No data corruption — just stuck in unrealized gains.

---

### Combo Strategy Removal (day 14)

Combo strategies (Long + Short in same wrapper) were blocked from new entries at day 14 and fully removed from `_build_strategies()` at day 17 once all positions closed.

**Combo total P&L over lifetime:** −$3.06
- Winners: ETH MACD Combo 1h (+$1.35), ETH MACD+RSI Combo 1h (+$1.28), SOL MACD Cross Combo 5m (+$1.04)
- Losers: SOL RSI Mom Combo 5m (−$2.15), XRP RSI Mom Combo 5m (−$1.48), ETH RSI Mom Combo 5m (−$1.49)

Pattern: MACD Combo 1h was profitable (tight signal + longer hold). RSI Mom Combo 5m was the primary drag (high churn, signals canceling). Net removal impact: approximately +$0.18/day freed up.

---

### Improvement Backlog

1. **Cut XRP/SOL RSI Mom Long 1h** — enough data, clear losers. Re-enable only if trend filter added (price > 200d EMA gates long entry).

2. **Reduce 5min position sizing** — currently 10% of balance, same as 1h. 5min generates 93% of trades for 16% of P&L. Halving to 5% reduces noise exposure while keeping short-side coverage during downtrends.

3. **Verify Short strategies post-fix** — all Short data before 2026-04-14 is invalid. Give Short strategies 2 weeks of real data before evaluating.

4. **RSI Rev Long strategy added (day 17)** — codifies the accidental Short-bug discovery. Entry: RSI crosses below 50 (dip-buy). Exit: RSI recovers above 55. Long-only on 1h across all 4 assets. Data collection starts 2026-04-14.

5. **Add trend filter to RSI Mom Long** — price > 200d EMA as gate. Infrastructure already exists from MACD+200d strategy. Would have filtered most of the XRP/SOL 1h RSI Mom losses (both in downtrend throughout the test period).

5. **TP/SL-only exit variants** — test a variant where only SL/TP/trail can close (no signal reversal exit). 91–96% of exits are already signal-based. The question is whether holding through signal reversals improves P&L or exposes more drawdown.

6. **SPY trend filter** — SPY close > 20d MA = risk-on, only enter longs in this regime. Would have reduced long entries during the April 2 tariff crash period.

7. **Max hold time** — zombie positions from day 1 are stuck open for 18+ days. A max hold time (e.g. 5 days on 5min, 14 days on 1h) would force resolution and free capital.

---

### Go-Live Considerations (day 30 target)

Current trigger: 1 month of profitable shadow trading.

- Realized P&L after 17 days: +$16.70 on $8,700 simulated = +0.19%
- Short direction bug invalidates risk management claims — need 2+ weeks of clean Short data before considering real capital
- Regime sensitivity is high: +$23 in 8 days of recovery, −$4 in 9 days of crash
- Suggested initial allocation: $500–1,000 real, concentrated on ETH/BTC 1h MACD strategies only, Long-only until Short strategies prove out post-fix
