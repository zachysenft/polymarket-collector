import os
import logging
import asyncio
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


def _send_webhook(embeds):
    """Fallback: send embeds via webhook if bot isn't available."""
    if not WEBHOOK_URL:
        return False
    try:
        payload = {"embeds": [e.to_dict() for e in embeds]}
        r = http_requests.post(WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Webhook send failed: {e}")
        return False


def _send_embed_sync(embeds):
    """Send embeds using bot token. Falls back to webhook."""
    if not BOT_TOKEN or not CHANNEL_ID:
        return _send_webhook(embeds)

    async def _send():
        intents = discord.Intents.default()
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            try:
                channel = client.get_channel(_get_channel_id())
                if not channel:
                    channel = await client.fetch_channel(_get_channel_id())
                for embed in embeds:
                    await channel.send(embed=embed)
            except Exception as e:
                log.error(f"Discord send failed: {e}")
            finally:
                await client.close()

        await client.start(BOT_TOKEN)

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_send())
        loop.close()
        return True
    except Exception as e:
        log.error(f"Discord bot send failed: {e}, trying webhook fallback")
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
    embed.set_footer(text="Daily backtest at 06:00 UTC | Type STOP to suspend")
    _send_embed_sync([embed])
    log.info("Startup message sent to Discord")


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

    embed.set_footer(text="Next run: tomorrow 06:00 UTC")

    _send_embed_sync([embed])
    log.info("Backtest summary sent to Discord")


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

    _send_embed_sync([embed])
    log.info("Trade breakdown sent to Discord")


def send_shadow_checkin():
    """Send 4x daily shadow trading status update."""
    if not BOT_TOKEN and not WEBHOOK_URL:
        return

    from db import (get_open_shadow_trades, get_closed_shadow_trades_since,
                    get_all_closed_shadow_trades, get_all_strategy_balances, get_latest_prices)
    from datetime import timedelta

    strategy_balances = get_all_strategy_balances()
    all_closed = get_all_closed_shadow_trades()
    open_positions = get_open_shadow_trades()

    total_balance = sum(strategy_balances.values()) if strategy_balances else 0
    num_strategies = len(strategy_balances) if strategy_balances else 0
    initial_total = 100.0 * num_strategies

    # Calculate realized P&L and win/loss
    total_pnl = sum(float(t["pnl_dollars"]) for t in all_closed)
    wins = sum(1 for t in all_closed if float(t["pnl_dollars"]) > 0)
    losses = sum(1 for t in all_closed if float(t["pnl_dollars"]) <= 0)

    # Determine shadow mode start day
    first_trade_ts = None
    if all_closed:
        first_trade_ts = all_closed[0].get("entry_ts")
    if open_positions and not first_trade_ts:
        first_trade_ts = open_positions[0].get("entry_ts")

    days_running = 0
    if first_trade_ts and hasattr(first_trade_ts, 'date'):
        days_running = (datetime.now(timezone.utc) - first_trade_ts).days

    color = 0x3498DB if total_pnl >= 0 else 0xFF0000
    embed = discord.Embed(
        title="Shadow Trading Check-In",
        description=(
            f"**Total Balance:** ${total_balance:.2f} (${initial_total:.0f} across {num_strategies} strategies) | "
            f"**Realized P&L:** ${total_pnl:+.2f} | "
            f"**W/L:** {wins}/{losses}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Open positions with current prices
    if open_positions:
        products = list(set(p["product"] for p in open_positions))
        current_prices = get_latest_prices(products)
        lines = []
        for pos in open_positions:
            curr_price = current_prices.get(pos["product"], float(pos["entry_price"]))
            entry_p = float(pos["entry_price"])
            size = float(pos["position_size"])
            unrealized_pct = (curr_price - entry_p) / entry_p
            unrealized_dollars = size * unrealized_pct
            lines.append(
                f"**{pos['strategy']}** | {pos['product']} {pos['side']} @ ${entry_p:,.2f}\n"
                f"  Now: ${curr_price:,.2f} | ${unrealized_dollars:+.2f} ({unrealized_pct*100:+.2f}%)"
            )
        embed.add_field(name=f"Open Positions ({len(open_positions)})",
                        value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Open Positions", value="None", inline=False)

    # Closed since last check-in (6 hours ago)
    since_ts = datetime.now(timezone.utc) - timedelta(hours=6)
    recent_closed = get_closed_shadow_trades_since(since_ts)
    if recent_closed:
        lines = []
        for t in recent_closed:
            lines.append(
                f"**{t['strategy']}** | {t['exit_reason']} @ ${float(t['exit_price']):,.2f} | "
                f"${float(t['pnl_dollars']):+.2f} ({float(t['pnl_pct']):+.2f}%)"
            )
        embed.add_field(name=f"Closed Since Last Check-In ({len(recent_closed)})",
                        value="\n".join(lines), inline=False)

    # Per-strategy stats (balance + cumulative P&L)
    strat_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    for t in all_closed:
        s = strat_stats[t["strategy"]]
        pnl = float(t["pnl_dollars"])
        if pnl > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] += pnl

    if strategy_balances:
        lines = []
        for name, bal in sorted(strategy_balances.items()):
            s = strat_stats.get(name, {"wins": 0, "losses": 0, "pnl": 0})
            lines.append(
                f"**{name}**: ${bal:.2f} | {s['wins']}W/{s['losses']}L  ${s['pnl']:+.2f}"
            )
        embed.add_field(name="Per-Strategy Balance & Stats", value="\n".join(lines), inline=False)

    footer = f"Shadow mode day {days_running}/30 | Go-live trigger: 1 month profitable"
    embed.set_footer(text=footer)

    _send_embed_sync([embed])
    log.info("Shadow check-in sent to Discord")


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
    _send_embed_sync([embed])
    log.info("Weekly shadow report sent to Discord")


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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(client.start(BOT_TOKEN))

    thread = threading.Thread(target=_run, daemon=True, name="discord-listener")
    thread.start()
    log.info("Discord listener thread started")
