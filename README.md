# trading-pipeline

Alert-first trading pipeline: it watches a list of symbols on Alpaca market
data and **notifies you when a position should be opened or closed**. It can
optionally mirror those decisions as orders on the Alpaca **paper** account.
It never places real-money orders.

## How it decides

On every **closed** bar, per symbol:

1. **Signal** (strategy view): the symbol's [strategy](#strategies) turns the
   indicator history into `BUY`, `SELL` or `HOLD`. The default, `sma_rsi`, uses an
   SMA 20/50 crossover, an RSI filter and a candlestick pattern bonus.
2. **Decision** (what *you* should do), combining the signal with your position
   (the table shows the default `sma_rsi` rules):

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
1Day · stock · bar 2026-06-25 · sma_rsi
Price: 122.55
Stop: 117.64 (-4.00%)   Target: 132.35 (+8.00%)
Why: sma_20_crossed_above_sma_50, rsi=69.2, bullish_candle_pattern=bullish_highwave
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
- **Fill prices:** with paper orders on, every cycle looks up the paper fills
  of recent entry and exit orders and stores them next to the alert prices
  (the bar close the decision used). The dashboard's P/L uses the fills once
  known and shows how far each fill was from the alert price, which is the
  slippage to compare against the backtest. Stop and target stay relative to
  the alert price, as in the backtest.

### Limiting open positions

`MAX_OPEN_POSITIONS=5` (or `--max-open-positions 5`) skips BUY signals while
5 positions are open across all symbols, virtual or paper; the skipped signal
is stored with reason `buy_signal_skipped_max_open_positions` but not alerted.
Exits are never blocked. Backtests run each symbol on its own, so they do not
apply this limit.

### Position size

`SIZING=risk` (default): each entry buys the amount that loses
`RISK_PER_TRADE` (default $10) if its stop is hit, capped at `MAX_NOTIONAL`
(default $1,000). A stop 4 % below the entry gives $250; an entry without a
stop buys the fixed `DEFAULT_NOTIONAL`. `SIZING=notional`: always
`DEFAULT_NOTIONAL`. The entry alert shows the size ("Size: $250.00, risking
$10.00 to the stop"), paper orders use it, alerts-only positions record it,
and backtests size entries the same way. Watchlist entries can override
`sizing`, `risk_per_trade`, `max_notional` and `notional`; the CLI has
`--sizing`, `--risk-per-trade`, `--max-notional` and `--notional`.

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

Strategy: `strategy = "..."` per `[[watch]]` entry or under `[defaults]` in
`watchlist.toml`; `--strategy` for a single symbol, and the watchlist default
(fallback: `DEFAULT_STRATEGY`, then `sma_rsi`). In a backtest, `--strategy`
runs that strategy on every watchlist entry.

Strategy parameters: `params = { fast = 10 }` per `[[watch]]` entry, or under
`[defaults]` for entries using the default strategy; `--param fast=10`
(repeatable) on the command line, for the entries running `--strategy`. Every
signal, position and backtest stores the full parameter set it ran with.

Stock data comes from the SIP feed (all US exchanges, `ALPACA_STOCK_FEED=sip`).
On the free plan Alpaca only serves SIP data older than 15 minutes, so stock
bars are requested up to 16 minutes ago and the watcher wakes up 16 minutes
after each bar boundary; daily alerts are unaffected. `iex` is real time but
sees only about 3 % of the market's volume.

Backtest (same decision logic, realistic default costs: stocks 0 commission +
5 bps spread, crypto 0.25 % commission + 5 bps spread):

```powershell
# Every watchlist entry (its own strategy, timeframe and notional), from 2021 on:
uv run trading-pipeline --backtest --start 2021-01-01
# One symbol, a fixed period:
uv run trading-pipeline --backtest --symbol AAPL --timeframe 1Day --start 2021-01-01 --end 2024-12-31
uv run trading-pipeline --backtest --symbol "BTC/USD" --asset-class crypto --timeframe 1Hour --limit 2000
```

- **History:** `--start`/`--end` pick the period to trade; earlier bars are
  fetched automatically to warm up the indicators. Without `--start`, the
  latest `--limit` bars are used (the first `min_bars` of them only warm up).
- **Sizing:** entries are sized exactly like paper orders (see
  [Position size](#position-size)), so the P/L per trade is comparable with
  paper trading. `Return [%]` is relative to `--cash`; look at `P/L [$]` and
  `Avg. Trade [%]`. `--position-size 0.95` sizes entries as a fraction of
  current equity instead (compounding).
- **Prices:** stock bars are split-adjusted (`ALPACA_ADJUSTMENT=split`), so a
  split no longer looks like a crash. `all` also adjusts for dividends.
- **Watchlist runs:** without `--symbol`, every watchlist entry is backtested
  on its own (own cash, no shared limits) and a summary table with the total
  P/L is printed. Every run is stored and shown in the dashboard.
- The stats' `Start` is the bar before the first tradable one (Backtesting.py
  needs one bar of context).

Parameter search (tune on some periods, check on later ones):

```powershell
uv run trading-pipeline --sweep --strategy donchian_breakout --watchlist watchlist.example.toml `
  --period 2015-01-01:2017-12-31 --period 2018-01-01:2020-12-31 --period 2021-01-01:2023-12-31 `
  --period 2024-01-01: --min-trades 150
uv run trading-pipeline --sweep --strategy indicathor --start 2021-01-01 --split 2024-01-01 --grid entry_score=1,2
```

Every `--grid` combination (default: a built-in grid per strategy) is
backtested on every watchlist entry in every *tune* period. All `--period`s but
the last `--check-periods` (default 1) are tune periods; without `--period`,
`--start`..`--split` tunes and `--split`..`--end` checks. Configurations are
ranked over the tune periods by `--rank-by` (`profit_factor` by default, or
`pnl`, `expectancy`), those that made money in every tune period first, and
need `--min-trades` trades to be finalists. The best `--top` finalists, the
strategy's defaults and the `sma_rsi` baseline then also run on the check
periods, which played no part in choosing them: a configuration that only
shines in the tune periods is overfitted.

Results are stored per configuration, period and entry, and shown in the
dashboard's **Sweeps** tab (P/L heatmaps per period and per symbol, and every
configuration in a table). Full backtests of the best `--detail-top` finalists
and of the defaults (trades, equity, chart) are stored per entry and period and
appear in the **Backtests** tab, labelled `sweep <id> <rank> <period>`.

Alpaca's free data starts in January 2016 for stocks and January 2021 for
crypto, so earlier periods are partial or empty (a period without data is
skipped for that entry).

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

Tests use a fake broker and notifier, never the network. GitHub Actions
(`.github/workflows/ci.yml`) runs both on every push to `main` and on every
pull request, on Python 3.11 and 3.13.

## Storage

SQLite (`DATABASE_PATH`, default `data/trading_pipeline.sqlite3`, git-ignored),
versioned with `PRAGMA user_version` and migrated automatically on start:

- `market_bars`, `feature_snapshots`: OHLCV and indicator values per bar
- `signals`: one row per processed bar: strategy, signal, decision, reason, order
  status, and the notification outbox (`notify_message`, `notified_at`, …)
- `positions`: virtual/paper positions with strategy, entry, stop, target, exit and reason
- `symbol_status`: watcher health per symbol
- `backtest_runs` (with strategy), `backtest_trades`, `backtest_equity_curve`

## Strategies

A strategy (`strategies/base.py`) produces signals and position rules;
entries and exits are always decided by `decisions.evaluate`/`decide`,
identically live and in the backtest. It defines:

- `name`: the key used in `watchlist.toml`, `--strategy` and the database
- `features`: indicators it reads, computed on top of `DEFAULT_FEATURES`
  (SMA 20/50, RSI 14, MACD, Bollinger Bands, ATR 14, candle patterns), which
  are always computed because the dashboard chart draws them
- `min_bars`: closed bars needed before the first signal
- `signal(symbol, asset_class, history)`: `BUY` / `SELL` / `HOLD` for
  `history.latest`. `history` holds the bars up to the evaluated one only, so
  the strategy cannot look ahead.
- `risk_levels(entry_price, latest)`: stop and target for a new long (either
  may be `None`); also used for positions adopted from the paper account

Optional hooks:

- `derive(frame, context)`: extra columns computed once over all bars (e.g. a
  score). They must be causal (a value at bar i uses only bars up to i); a
  test checks this for every registered strategy.
- `benchmark_for(symbol, asset_class)`: a symbol whose closes `derive` needs
  (e.g. SPY for relative strength), fetched alongside, live and in backtests.
- `manage(position, history)`: rules for an open position at each bar close: a
  new stop (trailing; only ever raised, and used from the next bar on) or an
  exit request (e.g. a maximum holding period).
- `stop_trigger`: `low` (a stop is hit when the bar's low touches it) or
  `close` (only a close beyond it counts).

Indicator kinds available to `features` (all computed by pandas-ta-classic):
`sma`, `ema`, `rsi`, `macd`, `bbands`, `atr`, `natr`, `adx`, `stoch`,
`donchian`, `roc`, `rvol` (volume relative to its average) and `candles`.

To add one: subclass `Strategy` as a frozen dataclass whose fields are its
parameters, register it in `strategies/__init__.py` (`STRATEGIES`), add tests,
then backtest it with `--strategy <name>`.

Registered strategies (all long-only; parameters are the dataclass fields):

- `sma_rsi` (default): deliberately simple, a starting point for validating the
  pipeline rather than a production strategy: SMA 20/50 crossover, RSI 14
  filter, candlestick confirmation, fixed −4 %/+8 % stop/target.
- `indicathor`: a momentum score from an RSI flag (≤30 −2, <50 −1, >50 +1,
  ≥70 +2) and a MACD flag (`macd_flag_mode="cross"`, the default: the original
  crossover events, +1 after a bullish cross below zero, −1 after a bearish
  cross above zero; `"state"`: above/below its signal line, ±2 when also on
  that side of zero), smoothed with an EMA. Entry when the score is ≥ `entry_score` with
  both flags positive and the trend filter (EMA 50 above EMA 200 by default,
  or close above EMA 200), ADX and optional relative-volume / relative-strength
  (vs SPY) filters agree. The score EMA is 10 bars by default; both defaults
  came out of the multi-period sweep. Exit when the score drops below `exit_score`, both flags are
  ≤ 0, or the ATR stop is hit; the stop starts 2× ATR(14) below the entry and
  trails at 3× ATR(10) under the highest high of the last 20 bars since entry.
- `rsi2_pullback`: buy when the close is above its SMA 200 and RSI(2) < 10;
  sell when the close is above its SMA 5. Optional 3× ATR stop and maximum
  holding period.
- `donchian_breakout`: buy when the close breaks the previous 55-bar high, sell
  when it breaks the previous 20-bar low; stop 2× ATR(20) below the entry.
