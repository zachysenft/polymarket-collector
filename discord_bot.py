import os
import logging
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import discord
import requests as http_requests

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
AUTHORIZED_USER_ID = os.environ.get("DISCORD_AUTHORIZED_USER_ID")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY")


def _get_channel_id():
    if CHANNEL_ID:
        return int(CHANNEL_ID)
    return None


def _post_with_retry(url, headers, payload):
    """POST to Discord with up to 2 retries, honouring Retry-After on 429."""
    for attempt in range(3):
        try:
            r = http_requests.post(url, headers=headers, json=payload, timeout=10)
            if r.status_code == 429:
                retry_after = float(r.json().get("retry_after", 5))
                log.warning(f"Discord 429 — waiting {retry_after:.1f}s (attempt {attempt + 1}/3)")
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            if attempt == 2:
                raise
            log.warning(f"Discord send error ({e}), retrying...")
    return False


def _send_webhook(embeds):
    """Fallback: send embeds via webhook if bot isn't available."""
    if not WEBHOOK_URL:
        return False
    try:
        payload = {"embeds": [e.to_dict() for e in embeds]}
        return _post_with_retry(WEBHOOK_URL, {}, payload)
    except Exception as e:
        log.error(f"Webhook send failed: {e}")
        return False


def _send_embed_sync(embeds):
    """Send embeds via Discord REST API. Falls back to webhook."""
    if BOT_TOKEN and CHANNEL_ID:
        try:
            payload = {"embeds": [e.to_dict() for e in embeds]}
            return _post_with_retry(
                f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
                {"Authorization": f"Bot {BOT_TOKEN}"},
                payload,
            )
        except Exception as e:
            log.error(f"Discord REST send failed: {e}, trying webhook fallback")
    return _send_webhook(embeds)


def send_startup_message():
    """Send a message on deploy/startup to confirm the service is alive."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        log.info("Discord not configured, skipping startup message")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title="Crypto Data Aggregator — Online",
        description=(
            f"Service started at **{now}**\n\n"
            "Running: backfill -> indicators -> backtest -> sweep -> collect\n"
            "Assets: BTC, ETH, SOL, XRP\n"
            "Timeframes: 5min, 1hour, 1day"
        ),
        color=0x00FF00,
    )
    embed.set_footer(text="Daily backtest at 12:00 UTC | Type STOP to suspend")
    ok = _send_embed_sync([embed])
    if ok:
        log.info("Startup message sent to Discord")
    else:
        log.error("Startup message FAILED to send — both REST and webhook unavailable")


def send_backtest_summary(bt_results, sweep_results):
    """Send daily backtest summary embed to Discord."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return

    # Count profitable combos
    profitable = [r for r in bt_results if r.get("trades", 0) > 0 and r.get("total_return", 0) > 0]
    total_combos = len([r for r in bt_results if r.get("trades", 0) > 0])
    sweep_profitable = [r for r in sweep_results if r.get("trades", 0) > 0 and r.get("total_return", 0) > 0]
    total_sweep = len([r for r in sweep_results if r.get("trades", 0) > 0])

    color = 0x00FF00 if profitable else 0xFF0000
    embed = discord.Embed(
        title="Daily Backtest Summary",
        description=(
            f"**{total_combos}** strategy combos + **{total_sweep}** sweep runs | "
            f"**{len(profitable)}** profitable strategies, **{len(sweep_profitable)}** profitable sweep configs"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Top 5 strategies
    if profitable:
        profitable.sort(key=lambda x: x["total_return"], reverse=True)
        lines = []
        for i, r in enumerate(profitable[:5], 1):
            lines.append(
                f"**{i}.** {r['product']} {r['granularity']} {r['strategy']} "
                f"-> **{r['total_return']:+.2f}%** ({r['trades']} trades, "
                f"{r['win_rate']:.0f}% win, maxDD={r['max_drawdown']:.2f}%)"
            )
        embed.add_field(name="Top Strategies", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Top Strategies", value="No profitable strategies this run", inline=False)

    # Top 3 sweep winners
    if sweep_profitable:
        sweep_profitable.sort(key=lambda x: x["total_return"], reverse=True)
        lines = []
        for i, r in enumerate(sweep_profitable[:3], 1):
            lines.append(
                f"**{i}.** {r['product']} {r['granularity']} {r['strategy']} "
                f"-> **{r['total_return']:+.2f}%**"
            )
        embed.add_field(name="Best Param Configs", value="\n".join(lines), inline=False)

    # Param label win count
    best_per_combo = defaultdict(lambda: None)
    for r in sweep_results:
        if r.get("trades", 0) == 0:
            continue
        base = r["strategy"].split(" [")[0]
        key = (base, r["product"], r["granularity"])
        if best_per_combo[key] is None or r["total_return"] > best_per_combo[key]["total_return"]:
            best_per_combo[key] = r

    label_wins = defaultdict(int)
    for v in best_per_combo.values():
        if v and v.get("total_return", 0) > 0 and "[" in v["strategy"]:
            label = v["strategy"].split("[")[1].rstrip("]")
            label_wins[label] += 1

    if label_wins:
        parts = [f"**{label}**: {count}" for label, count in sorted(label_wins.items(), key=lambda x: -x[1])]
        embed.add_field(name="Param Win Count", value=" | ".join(parts), inline=False)

    embed.set_footer(text="Next run: tomorrow 12:00 UTC")

    ok = _send_embed_sync([embed])
    if ok:
        log.info("Backtest summary sent to Discord")
    else:
        log.error("Backtest summary FAILED to send — both REST and webhook unavailable")


def send_trade_breakdown(bt_results):
    """Send per-asset trade breakdown embed to Discord."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return

    active = [r for r in bt_results if r.get("trades", 0) > 0]
    if not active:
        return

    embed = discord.Embed(
        title="Trade Breakdown by Asset",
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )

    products = sorted(set(r["product"] for r in active))
    for product in products:
        prod_results = [r for r in active if r["product"] == product]
        lines = []

        # Best strategy per timeframe
        for gran in ["5min", "1hour", "1day"]:
            gran_results = [r for r in prod_results if r["granularity"] == gran]
            if gran_results:
                best = max(gran_results, key=lambda x: x["total_return"])
                lines.append(
                    f"**{gran}**: {best['strategy']} "
                    f"{best['total_return']:+.2f}% ({best['trades']} trades)"
                )

        # Exit type distribution
        total_sl = sum(r.get("sl_exits", 0) for r in prod_results)
        total_tp = sum(r.get("tp_exits", 0) for r in prod_results)
        total_trail = sum(r.get("trail_exits", 0) for r in prod_results)
        total_signal = sum(r.get("signal_exits", 0) for r in prod_results)
        total_exits = total_sl + total_tp + total_trail + total_signal

        if total_exits > 0:
            lines.append(
                f"Exits: SL {total_sl/total_exits*100:.0f}% | "
                f"TP {total_tp/total_exits*100:.0f}% | "
                f"Trail {total_trail/total_exits*100:.0f}% | "
                f"Signal {total_signal/total_exits*100:.0f}%"
            )

        if lines:
            embed.add_field(name=product, value="\n".join(lines), inline=False)

    ok = _send_embed_sync([embed])
    if ok:
        log.info("Trade breakdown sent to Discord")
    else:
        log.error("Trade breakdown FAILED to send — both REST and webhook unavailable")


def send_regime_alert(vix_now, vix_prev):
    """Send a Discord alert when VIX crosses the 25 threshold in either direction."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return
    crossed_up = vix_prev < 25 and vix_now >= 25
    crossed_down = vix_prev >= 25 and vix_now < 25
    if not crossed_up and not crossed_down:
        return
    if crossed_down:
        title = "✅ Regime Change: Fear Subsiding"
        desc = (f"VIX dropped below 25 ({vix_prev:.1f} → **{vix_now:.1f}**)\n"
                f"MACD+VIX entries now **active** — regime-filtered strategies live.")
        color = 0x00AA00
    else:
        title = "⚠️ Regime Change: Fear Spike"
        desc = (f"VIX crossed above 25 ({vix_prev:.1f} → **{vix_now:.1f}**)\n"
                f"MACD+VIX entries **paused** — waiting for fear to subside.")
        color = 0xFF8800
    embed = discord.Embed(title=title, description=desc, color=color,
                          timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="VIX threshold: 25 | checked daily at 12:15 UTC")
    ok = _send_embed_sync([embed])
    if ok:
        log.info(f"Regime alert sent: VIX {vix_prev:.1f} → {vix_now:.1f}")
    else:
        log.error("Regime alert FAILED to send — both REST and webhook unavailable")


def send_shadow_checkin():
    """Send 4x daily shadow trading status update."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return

    from db import (get_open_shadow_trades, get_closed_shadow_trades_since,
                    get_all_closed_shadow_trades, get_all_strategy_balances, get_latest_prices,
                    get_last_checkin_snapshot, save_checkin_snapshot)
    from datetime import timedelta

    strategy_balances = get_all_strategy_balances()
    prev_snapshot_ts, prev_snapshot = get_last_checkin_snapshot()
    save_checkin_snapshot(strategy_balances)
    all_closed = get_all_closed_shadow_trades()
    open_positions = get_open_shadow_trades()
    since_ts = datetime.now(timezone.utc) - timedelta(hours=6)
    recent_closed = get_closed_shadow_trades_since(since_ts)

    num_strategies = len(strategy_balances)
    total_balance = sum(strategy_balances.values()) if strategy_balances else 0
    initial_total = 100.0 * num_strategies
    total_pnl = sum(float(t["pnl_dollars"]) for t in all_closed)
    wins = sum(1 for t in all_closed if float(t["pnl_dollars"]) > 0)
    losses = sum(1 for t in all_closed if float(t["pnl_dollars"]) <= 0)
    total_trades = wins + losses
    win_pct = f" ({wins/total_trades*100:.1f}%)" if total_trades > 0 else ""

    # Days running
    all_trades = all_closed + open_positions
    first_ts = min((t.get("entry_ts") for t in all_trades if t.get("entry_ts")), default=None)
    days_running = (datetime.now(timezone.utc) - first_ts).days if first_ts and hasattr(first_ts, "date") else 0

    color = 0x00AA00 if total_pnl >= 0 else 0xFF4444
    embed = discord.Embed(
        title="Shadow Trading Check-In",
        description=(
            f"**{num_strategies} strategies** | Portfolio: **${total_balance:.2f}** / ${initial_total:.0f} started\n"
            f"All-time realized P&L: **${total_pnl:+.2f}** | {wins}W / {losses}L{win_pct} | "
            f"{len(open_positions)} open now"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    def _shorten(name):
        return (name.replace("Long", "L").replace("Short", "S").replace("Combo", "C")
                    .replace("1hour", "1h").replace("5min", "5m").replace("1day", "1d"))

    def _fmt_trade(t):
        entry_p = float(t["entry_price"])
        exit_p = float(t["exit_price"])
        pnl = float(t["pnl_dollars"])
        sign = "+" if pnl >= 0 else ""
        return (f"`{_shorten(t['strategy'])}` {t['side'].upper()} {t['product']} "
                f"${entry_p:,.0f}→${exit_p:,.0f} [{t['exit_reason'].upper()}] **{sign}${pnl:.2f}**")

    # --- Completed trades: count + top 5 gainers + top 5 losers ---
    if recent_closed:
        sorted_by_pnl = sorted(recent_closed, key=lambda t: float(t["pnl_dollars"]), reverse=True)
        top5 = sorted_by_pnl[:5]
        bot5 = sorted_by_pnl[-5:] if len(sorted_by_pnl) > 5 else []

        embed.add_field(
            name=f"Completed Trades — Last 6h ({len(recent_closed)})",
            value=f"Top gainers / biggest losers shown below",
            inline=False,
        )
        embed.add_field(
            name="📈 Top 5 Gainers",
            value="\n".join(_fmt_trade(t) for t in top5) or "—",
            inline=True,
        )
        if bot5:
            embed.add_field(
                name="📉 Top 5 Losers",
                value="\n".join(_fmt_trade(t) for t in reversed(bot5)) or "—",
                inline=True,
            )
    else:
        embed.add_field(name="Completed Trades — Last 6h", value="No trades closed this window", inline=False)

    # --- Open positions: count + unrealized + top 5 winners/losers ---
    if open_positions:
        products = list({p["product"] for p in open_positions})
        current_prices = get_latest_prices(products)

        scored = []
        total_unrealized = 0.0
        for pos in open_positions:
            curr_price = current_prices.get(pos["product"], float(pos["entry_price"]))
            entry_p = float(pos["entry_price"])
            size = float(pos["position_size"])
            if pos["side"] == "long":
                unreal_dollars = size * (curr_price - entry_p) / entry_p
            else:
                unreal_dollars = size * (entry_p - curr_price) / entry_p
            total_unrealized += unreal_dollars
            scored.append((pos, curr_price, unreal_dollars))

        scored.sort(key=lambda x: x[2], reverse=True)
        sign = "+" if total_unrealized >= 0 else ""

        def _fmt_open(pos, curr_price, unreal):
            entry_p = float(pos["entry_price"])
            s = "+" if unreal >= 0 else ""
            return (f"`{_shorten(pos['strategy'])}` {pos['side'].upper()} {pos['product']} "
                    f"${entry_p:,.0f}→${curr_price:,.0f} **{s}${unreal:.2f}**")

        embed.add_field(
            name=f"Open Positions ({len(open_positions)})  unrealized {sign}${total_unrealized:.2f}",
            value="Top winners / biggest losers shown below",
            inline=False,
        )
        embed.add_field(
            name="🟢 Top 5 Open Winners",
            value="\n".join(_fmt_open(*x) for x in scored[:5]) or "—",
            inline=True,
        )
        if len(scored) > 5:
            embed.add_field(
                name="🔴 Top 5 Open Losers",
                value="\n".join(_fmt_open(*x) for x in scored[-5:]) or "—",
                inline=True,
            )

    # --- Leaderboard: top 5 per timeframe with delta since last check-in ---
    if strategy_balances:
        hours_since = None
        if prev_snapshot_ts:
            elapsed = (datetime.now(timezone.utc) - prev_snapshot_ts).total_seconds() / 3600
            hours_since = round(elapsed, 1)

        def _fmt_lb(n, b):
            pnl = b - 100
            sign = "+" if pnl >= 0 else ""
            line = f"`{_shorten(n)}` {sign}${pnl:.2f}"
            if hours_since is not None and n in prev_snapshot:
                delta = b - prev_snapshot[n]
                if delta != 0:
                    arrow = "↑" if delta > 0 else "↓"
                    line += f"  {arrow}${abs(delta):.2f} in {hours_since}h"
            return line

        for gran_label, gran_key in [("5m", "5min"), ("1h", "1hour"), ("1d", "1day")]:
            gran_strats = [(n, b) for n, b in strategy_balances.items() if gran_key in n]
            if not gran_strats:
                continue
            top5 = sorted(gran_strats, key=lambda x: x[1], reverse=True)[:5]
            embed.add_field(
                name=f"🏆 Top 5 — {gran_label}",
                value="\n".join(_fmt_lb(n, b) for n, b in top5),
                inline=True,
            )

    embed.set_footer(text=f"Shadow mode day {days_running}/30 | Go-live trigger: 1 month profitable")

    ok = _send_embed_sync([embed])
    if ok:
        log.info("Shadow check-in sent to Discord")
    else:
        log.error("Shadow check-in FAILED to send — both REST and webhook unavailable")


def send_weekly_report():
    """Send Sunday weekly shadow trading performance summary."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return

    from db import (get_closed_shadow_trades_since, get_all_closed_shadow_trades,
                    get_all_strategy_balances)
    from datetime import timedelta

    strategy_balances = get_all_strategy_balances()
    since_ts = datetime.now(timezone.utc) - timedelta(days=7)
    weekly_trades = get_closed_shadow_trades_since(since_ts)
    all_closed = get_all_closed_shadow_trades()

    total_balance = sum(strategy_balances.values()) if strategy_balances else 0
    num_strategies = len(strategy_balances) if strategy_balances else 0
    initial_total = 100.0 * num_strategies

    weekly_pnl = sum(float(t["pnl_dollars"]) for t in weekly_trades)
    weekly_wins = sum(1 for t in weekly_trades if float(t["pnl_dollars"]) > 0)
    weekly_losses = sum(1 for t in weekly_trades if float(t["pnl_dollars"]) <= 0)
    all_time_pnl = sum(float(t["pnl_dollars"]) for t in all_closed)

    color = 0x00FF00 if weekly_pnl >= 0 else 0xFF0000
    embed = discord.Embed(
        title="Weekly Shadow Trading Report",
        description=(
            f"**Total Balance:** ${total_balance:.2f} / ${initial_total:.0f} started\n"
            f"**Week P&L:** ${weekly_pnl:+.2f} ({len(weekly_trades)} trades, {weekly_wins}W/{weekly_losses}L) | "
            f"**All-Time P&L:** ${all_time_pnl:+.2f}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Per-strategy balances
    if strategy_balances:
        lines = []
        strat_pnl = defaultdict(float)
        for t in all_closed:
            strat_pnl[t["strategy"]] += float(t["pnl_dollars"])
        for name, bal in sorted(strategy_balances.items()):
            pnl = bal - 100.0
            lines.append(f"**{name}**: ${bal:.2f} ({pnl:+.2f} all-time)")
        embed.add_field(name="Strategy Balances", value="\n".join(lines), inline=False)

    # Best and worst trade of the week
    if weekly_trades:
        best = max(weekly_trades, key=lambda t: float(t["pnl_dollars"]))
        worst = min(weekly_trades, key=lambda t: float(t["pnl_dollars"]))
        lines = [
            f"**Best:** {best['strategy']} — {best['exit_reason']} ${float(best['pnl_dollars']):+.2f} ({float(best['pnl_pct']):+.2f}%)",
            f"**Worst:** {worst['strategy']} — {worst['exit_reason']} ${float(worst['pnl_dollars']):+.2f} ({float(worst['pnl_pct']):+.2f}%)",
        ]
        embed.add_field(name=f"Week Highlights ({len(weekly_trades)} trades closed)",
                        value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Week Trades", value="No trades closed this week", inline=False)

    embed.set_footer(text="Weekly summary — every Sunday 09:00 UTC")
    ok = _send_embed_sync([embed])
    if ok:
        log.info("Weekly shadow report sent to Discord")
    else:
        log.error("Weekly shadow report FAILED to send — both REST and webhook unavailable")


def start_discord_listener():
    """Start a persistent Discord bot that listens for STOP command."""
    if not BOT_TOKEN or not CHANNEL_ID:
        log.info("Discord listener not started (missing BOT_TOKEN or CHANNEL_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info(f"Discord listener connected as {client.user}")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if str(message.channel.id) != str(CHANNEL_ID):
            return
        if message.content.strip().upper() != "STOP":
            return

        # Check authorization
        if AUTHORIZED_USER_ID and str(message.author.id) != str(AUTHORIZED_USER_ID):
            await message.reply("Not authorized to stop the service.")
            return

        await message.reply("Stopping service...")
        log.warning("STOP command received from Discord — shutting down")

        # Try Render API suspend first
        if RENDER_SERVICE_ID and RENDER_API_KEY:
            try:
                r = http_requests.post(
                    f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/suspend",
                    headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
                    timeout=10,
                )
                if r.status_code in (200, 202):
                    await message.channel.send("Render service suspended. Resume manually from dashboard.")
                    log.info("Render service suspended via API")
                    return
                else:
                    log.warning(f"Render suspend returned {r.status_code}, falling back to exit")
            except Exception as e:
                log.error(f"Render suspend failed: {e}, falling back to exit")

        # Fallback: hard exit
        await message.channel.send("Forcing process exit (Render will show as crashed).")
        os._exit(1)

    def _run():
        client.run(BOT_TOKEN, log_handler=None)

    thread = threading.Thread(target=_run, daemon=True, name="discord-listener")
    thread.start()
    log.info("Discord listener thread started")
