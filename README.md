# trading-pipeline

Alert-first trading pipeline: it watches a list of symbols on Alpaca market
data and **notifies you when a position should be opened or closed**. It can
optionally mirror those decisions as orders on the Alpaca **paper** account.
It never places real-money orders.

## How it decides

On every **closed** bar, per symbol:

1. **Signal** (strategy view): SMA 20/50 crossover, RSI filter, candlestick
   pattern bonus → `BUY`, `SELL` or `HOLD`.
2. **Decision** (what *you* should do), combining the signal with your position:

   | You are | Signal / bar | Decision → alert |
   |---|---|---|
   | flat | BUY (SMA20 crosses above SMA50, RSI < 70) | 🟢 **enter long**, with stop −4 % and target +8 % |
   | long | bar low ≤ stop | 🔴 **exit**: `stop_loss_hit` |
   | long | bar high ≥ target | 🔴 **exit**: `take_profit_hit` |
   | long | SELL (SMA20 crosses below SMA50, or RSI > 75) | 🔴 **exit**: `sell_signal` |
   | otherwise | anything | no alert (stored as HOLD/none) |

The backtest runs exactly the same decision code, so alerts and backtest
trades line up (the backtest fills on the next bar's open, which is what you
get when acting on an alert).

Example alert (illustrative values):

```text
🟢 BUY AAPL — enter long
1Day · stock · bar 2026-06-25
Price: 122.55
Stop: 117.64 (-4.00%)   Target: 132.35 (+8.00%)
Why: sma20_crossed_above_sma50, rsi=69.2, bullish_candle_pattern=bullish_highwave
Confidence: 0.75
Paper order: not placed (alerts only)
```

### Reliability guarantees

- Only completed bars are evaluated; a forming bar never triggers an alert.
- Every closed bar is evaluated once, in order. Bars missed during downtime
  or a failed cycle are replayed on the next run (up to 50), so a crossover
  or stop touch is never skipped; those alerts are marked as late. The very
  first run for a symbol only looks at the latest bar.
- Each bar is stored under a unique key: reruns never duplicate alerts or orders.
- Failed notifications stay in an outbox and are retried every cycle for 24
  hours; anything still undeliverable is reported in one summary message.
- One failing symbol, or a database hiccup, never stops the watcher; errors are
  alerted (at most once per hour per symbol) and shown in the dashboard.

### Where "am I in a position?" comes from

- **Alerts only (default):** a *virtual* position per symbol/timeframe, opened
  on each BUY alert at the bar close. This assumes you act on every alert.
- **Paper orders on:** the Alpaca paper account is the source of truth for
  positions the pipeline opened there. It adopts paper positions it didn't
  open (stop/target from the risk rules, checked from the next bar), tells
  you when it stops tracking one you closed yourself, cancels a still-queued
  entry when an exit comes first, and won't re-adopt a position whose exit
  order is still queued. Virtual positions from alerts-only mode keep getting
  exit alerts after you switch orders on.

## Safety model

```text
TRADING_MODE=alerts_only   # default: never submits orders
PLACE_ORDERS=false
```

Paper orders are submitted only when **all three** are set:

```text
ALPACA_PAPER=true
TRADING_MODE=paper
PLACE_ORDERS=true
```

With `ALPACA_PAPER=false` the broker adapter never creates a trading client,
so even a bug cannot send a live order.

## Setup (Windows PowerShell)

```powershell
uv sync --extra dev            # or: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e .[dev]
Copy-Item .env.example .env    # fill in ALPACA_* (paper keys) and TELEGRAM_*
Copy-Item watchlist.example.toml watchlist.toml
```

The app reads `.env` automatically from the working directory.

### Telegram

1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into
   `TELEGRAM_CHAT_ID`.
3. Test: `uv run trading-pipeline --once --telegram --notify-hold`

## Run

Continuous alerts over the watchlist (recommended):

```powershell
uv run trading-pipeline --watch --telegram
```

It checks just after each bar closes (interval = shortest timeframe, between
1 and 15 minutes; override with `--interval 300`), sends a start/stop message,
and keeps running until Ctrl+C.

To start it automatically, create a Windows Task Scheduler task with trigger
"At log on", action `uv`, arguments `run trading-pipeline --watch --telegram`,
and "Start in" set to the repo folder. Alternatively, schedule
`uv run trading-pipeline --once --telegram` every few minutes; reruns are
idempotent.

Single symbol, one pass:

```powershell
uv run trading-pipeline --symbol AAPL --asset-class stock --timeframe 1Day --telegram
uv run trading-pipeline --symbol "BTC/USD" --asset-class crypto --timeframe 1Hour
```

Timeframes: `1Min`–`59Min`, `1Hour`–`23Hour`, `1Day`.

Backtest (same decision logic, realistic default costs: stocks 0 commission +
5 bps spread, crypto 0.25 % commission + 5 bps spread):

```powershell
uv run trading-pipeline --backtest --symbol AAPL --timeframe 1Day --limit 1000
uv run trading-pipeline --backtest --symbol "BTC/USD" --asset-class crypto --timeframe 1Hour --limit 2000
```

## Dashboard

```powershell
uv run trading-dashboard --server.port 8501
```

Auto-refreshing Streamlit app over the same SQLite file:

- **Overview**: open positions with P/L and distance to stop/target, recent
  alerts with delivery status, watcher health (errors, stale runs).
- **Decisions**: every entry/exit alert, the exact message sent, and the
  indicators on that bar.
- **Positions**: open and closed positions, win rate, average P/L.
- **Chart**: candles, SMA/BB/RSI/MACD/ATR, alert markers, and entry/stop/target
  levels for each position.
- **Signals**: every processed bar, including HOLDs.
- **Backtests**: runs, equity, drawdown, trade P/L and trade overlay.

## Development

```powershell
uv run python -m pytest -q
uv run ruff check src tests
```

Tests use a fake broker and notifier, never the network. To run both on every
push, add `.github/workflows/ci.yml` (`uv sync --extra dev`, then the two
commands above).

## Storage

SQLite (`DATABASE_PATH`, default `data/trading_pipeline.sqlite3`, git-ignored),
versioned with `PRAGMA user_version` and migrated automatically on start:

- `market_bars`, `feature_snapshots`: OHLCV and indicator values per bar
- `signals`: one row per processed bar: signal, decision, reason, order
  status, and the notification outbox (`notify_message`, `notified_at`, …)
- `positions`: virtual/paper positions with entry, stop, target, exit and reason
- `symbol_status`: watcher health per symbol
- `backtest_runs`, `backtest_trades`, `backtest_equity_curve`

## Current strategy

Deliberately simple, a starting point for validating the pipeline rather than a
production strategy: SMA 20/50 crossover, RSI 14 filter, pandas-ta-classic
candlestick confirmation, fixed −4 %/+8 % stop/target. MACD, Bollinger Bands
and ATR are already computed for future strategy and risk work.
