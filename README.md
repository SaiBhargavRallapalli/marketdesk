# 📊 MarketDesk

**A self-hosted, free alternative to a Trendlyne membership** — built for
daily Indian-equity research: DVS composite scores, a 20-parameter screener,
backtesting, portfolio tracking, HTML reports and Telegram/email alerts.

> Model scores and backtests are research aids, **not investment advice**.
> Free data sources (Yahoo/NSE) may be delayed or incomplete.

## Feature map: Trendlyne → MarketDesk

| Trendlyne (paid) | MarketDesk (free) | Module |
|---|---|---|
| DVM score | **DVS score** — transparent weighted percentile (value 25 / quality 25 / momentum 30 / growth 10 / dividend 10) | `scores.py` |
| Stock screeners + alerts | 20 range-filters, presets, save/load, CSV export, alert rules | `screener.py`, `alerts.py` |
| Backtesting | SMA cross, RSI reversion, momentum rotation, buy & hold — with costs, Sharpe/Sortino/max-DD | `backtest.py` |
| Portfolio report | Valuation, P&L, sector/weight allocation, vs-NIFTY curve, concentration health | `portfolio.py` |
| Forecaster / analyst targets | Yahoo consensus target, upside, analyst count, next earnings | `fundamentals.py` |
| Stock reports (300/mo) | HTML one-pagers: candlestick + DMAs, fundamentals, DVS breakdown | `reports.py` |
| Daily alerts | Telegram bot + email (SMTP), 6 rule types, cooldowns, log | `alerts.py` |
| Market breadth | Advances/declines/unchanged gauge across the universe | `market.py` |
| Data downloader | CSV export on every table | dashboard buttons |
| Broker integration | Pluggable adapters: manual (default), Dhan, Angel One, Shoonya, Zerodha Kite | `brokers/` |
| MarketMind AI | *(not replicated — no free LLM; ask your own chatbot)* | — |

## Setup (first time)

```bash
cd marketdesk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # optional: Telegram/SMTP/broker keys
```

## Deploy

Three supported targets — all run the same Docker image
(`Dockerfile` + `docker/entrypoint.sh`: dashboard in front, daily data
pipeline looping in the background, SQLite persisted on a mounted volume).

### A. Docker locally

```bash
docker compose up -d          # http://localhost:8501
docker compose logs -f        # watch the first-time data fetch
```

### B. Render (render.yaml)

1. Push this repo to GitHub (already done for `SaiBhargavRallapalli/marketdesk`).
2. [render.com](https://render.com) → **New → Blueprint** → pick the repo.
   Render reads `render.yaml`: Docker runtime, Singapore region,
   health check `/_stcore/health`, 1 GB persistent disk at `/app/data`.
3. Free tier: delete the `plan:` + `disk:` blocks from `render.yaml` first —
   the app then re-fetches data in the background on every deploy
   (~10-15 min for the first Nifty 500 pull).
4. Secrets (Telegram/SMTP): add as env vars in the Render dashboard.

### C. Fly.io (fly.toml)

```bash
flyctl auth login
flyctl apps create marketdesk
flyctl volumes create marketdesk_data --region bom --size 1   # Mumbai volume
flyctl deploy                                               # builds Dockerfile
flyctl secrets set TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx   # optional
```

`fly.toml` auto-stops the machine when idle (saves cost), binds a Mumbai
volume to `/app/data`, and sets `TZ=Asia/Kolkata`.

**First boot on any host:** the dashboard starts immediately; the background
pipeline fills the empty DB over the first ~10-15 minutes, then refreshes
every 6 hours. Upload an existing `data/marketdesk.db` via volume/file to
skip the initial fetch.

## Daily use

```bash
# 1. Pull data + recompute scores (Nifty 500 first run ~15-30 min; daily refresh ~2-5 min)
.venv/bin/python -m marketdesk.jobs update            # add --universe nifty50 for a fast start
.venv/bin/python -m marketdesk.jobs scores

# 2. Open the dashboard
.venv/bin/streamlit run app.py

# 3. Test alert channels (once .env is filled)
.venv/bin/python -m marketdesk.jobs alerts --test

# 4. Full pipeline: update → scores → alerts → report
.venv/bin/python -m marketdesk.jobs daily
```

Schedule it with cron (e.g. weekdays 9 pm):

```
0 21 * * 1-5 cd /path/to/marketdesk && .venv/bin/python -m marketdesk.jobs daily >> data/pipeline.log 2>&1
```

macOS launchd works too; the Settings tab can also run the pipeline manually.

## Dashboard tabs

- **🏠 Dashboard** — index snapshot, breadth gauge, top gainers/losers, top DVS, portfolio card
- **🔍 Screener** — presets (Quality compounders, Value, Momentum, High dividend…), slider filters, save/load, CSV
- **🧪 Backtest** — strategy + basket + period, equity curve vs benchmark, full metrics
- **💼 Portfolio** — add holdings or import from a configured broker, allocation pies, portfolio vs NIFTY
- **📈 Stock** — search any stock: chart, RSI/DMAs, fundamentals, DVS breakdown, HTML report
- **📰 Reports** — generate/view/download daily & stock HTML reports
- **🔔 Alerts** — rule builder (price, % move, RSI, DVS, earnings), test send, log
- **⚙️ Settings** — write Telegram/SMTP/broker keys into `.env`, data status, run pipeline

## Alert setup (5 minutes)

1. **Telegram:** message [@BotFather](https://t.me/BotFather) → `/newbot` → copy token;
   message your bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
2. **Email:** Gmail → Security → 2-Step Verification → [App Passwords](https://myaccount.google.com/apppasswords).
3. Paste both into **⚙️ Settings** (or `.env`), then **🧪 Send test message**.

## Broker APIs (optional)

Default is **manual holdings** — nothing else needed. To auto-import:

| Broker | .env keys | Status |
|---|---|---|
| Dhan | `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` | implemented, needs your keys |
| Angel One | `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD` | implemented, needs your keys |
| Shoonya | `SHOONYA_USER_ID`, `SHOONYA_PASSWORD` | best-effort — verify against current docs |
| Zerodha | `KITE_API_KEY`, `KITE_ACCESS_TOKEN` | needs paid Kite Connect + `pip install kiteconnect` |

New broker: subclass `marketdesk/brokers/base.py::Broker` and register in
`brokers/__init__.py`.

## Data sources

- **Prices / indices:** Yahoo Finance (`.NS` tickers), 2-year rolling window, cached in SQLite
- **Universe:** NSE `ind_nifty50/500list.csv` (auto-refreshed weekly, falls back to niftyindices.com, then your `data/universe_*.csv`)
- **Fundamentals & analyst targets:** Yahoo Finance `quoteSummary`
- **Storage:** single file `data/marketdesk.db` (SQLite)

## Project layout

```
marketdesk/
├── app.py                  # Streamlit dashboard (all tabs)
├── Dockerfile              # container image (Render / Fly / local Docker)
├── docker-compose.yml      # one-command local run
├── docker/entrypoint.sh    # dashboard + 6-hourly data pipeline loop
├── render.yaml             # Render blueprint (Singapore + disk)
├── fly.toml                # Fly.io app (Mumbai volume + auto-stop)
├── .streamlit/config.toml  # server config for local + container runs
├── config.yaml             # scoring weights, backtest defaults, cooldowns
├── .env.example            # channels + broker keys template
├── marketdesk/
│   ├── config.py db.py     # config + SQLite layer
│   ├── universe.py         # NSE constituent lists
│   ├── market.py           # downloads, indicators, breadth, movers
│   ├── fundamentals.py     # Yahoo fundamentals (normalised to %)
│   ├── scores.py           # DVS composite
│   ├── screener.py         # filters, presets, save/load
│   ├── backtest.py         # 4 strategies + metrics
│   ├── portfolio.py        # valuation, allocation, health
│   ├── reports.py          # daily + stock HTML reports
│   ├── alerts.py           # rules, Telegram, email
│   ├── jobs.py             # CLI: update|scores|alerts|report|daily
│   └── brokers/            # pluggable broker adapters
└── data/ reports/          # db, caches, generated HTML
```

## Limitations

- Free data can be delayed (Yahoo ~15 min for NSE), occasionally flaky, and
  lacks intraday history — fine for daily investing, not for HFT.
- DVS is a *transparent* composite, not Trendlyne's proprietary DVM; expect
  similar-but-not-identical rankings.
- Broker adapters are untested until you add real credentials.
- Backtests are long-only and point-and-time; they do not model intraday fills.
