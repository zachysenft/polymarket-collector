# Polymarket Lag Research Collector

Collects BTC price (Binance websocket), funding rates, and Polymarket
probabilities every 2 minutes. Purpose: prove or disprove the lag thesis
before building an arb bot.

## What It Collects

| Data | Source | Frequency |
|------|--------|-----------|
| BTC spot price | Binance websocket | Every 60s |
| Funding rate | Binance Futures REST | Every 5 min |
| Polymarket BTC market probabilities | Polymarket CLOB API | Every 2 min |

## Local Setup

```bash
# 1. Clone / cd into project
cd polymarket-collector

# 2. Copy env file
cp .env.example .env
# Edit .env with your Supabase DATABASE_URL

# 3. Build and run
docker build -t poly-collector .
docker run --env-file .env poly-collector
```

You should see logs like:
```
2026-03-25 20:00:01 [INFO] Schema initialized
2026-03-25 20:00:02 [INFO] BTC websocket connected
2026-03-25 20:00:03 [INFO] BTC: $87,250.00 | 24h vol: 42,300 BTC
2026-03-25 20:00:05 [INFO] Funding rate: 0.0082% | Next: 20:00 UTC
2026-03-25 20:00:07 [INFO] Polymarket snapshot: 8 BTC markets captured
```

## Deploy to Render

1. Push this folder to a GitHub repo
2. Go to render.com → New → **Background Worker** (not Web Service)
3. Connect your GitHub repo
4. Runtime: **Docker**
5. Add environment variable:
   - `DATABASE_URL` → your Supabase connection string
6. Deploy

Render Background Workers run 24/7 with no spin-down on the free tier.

## Supabase Setup

1. Go to supabase.com → your project → SQL Editor
2. The schema is auto-created on first run via `init_schema()`
3. Get your connection string: Settings → Database → Connection string (URI mode)
4. Use the **direct connection** string, not the pooler, for this use case

## After 1-2 Weeks: Run the Analysis

Open `analysis_queries.sql` in Supabase SQL editor.

Start with Query 5 (health check) to confirm data is clean, then run
Query 4 (peak mispricing window) — that's the key number.

**What you're looking for:**
- Average lag > 3 minutes after a BTC move → tradeable window exists
- Average prob drift > 5% at peak lag → edge covers gas + spread
- Consistent pattern across multiple move sizes → not random noise

If those three conditions hold, you have a thesis worth building on.
