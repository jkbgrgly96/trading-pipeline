# trading-pipeline

Alert-first trading pipeline. The first broker adapter is Alpaca paper trading;
the broker interface is intentionally small so an IBKR adapter can be added
later without rewriting strategy code.

## Safety model

The default mode never submits orders:

```text
TRADING_MODE=alerts_only
PLACE_ORDERS=false
```

Paper orders are submitted only when all three are set:

```text
ALPACA_PAPER=true
TRADING_MODE=paper
PLACE_ORDERS=true
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item .env.example .env
```

Fill in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env`.
The app reads `.env` automatically; exporting variables manually also works.

Runs persist to SQLite by default:

```text
DATABASE_PATH=data/trading_pipeline.sqlite3
```

## Run

Stock example:

```powershell
$env:ALPACA_API_KEY="..."
$env:ALPACA_SECRET_KEY="..."
trading-pipeline --symbol AAPL --asset-class stock --timeframe 1Day --limit 250
```

Crypto example:

```powershell
$env:ALPACA_API_KEY="..."
$env:ALPACA_SECRET_KEY="..."
trading-pipeline --symbol "BTC/USD" --asset-class crypto --timeframe 1Hour --limit 250
```

Backtest example:

```powershell
trading-pipeline --backtest --symbol AAPL --asset-class stock --timeframe 1Day --limit 1000 --cash 10000 --commission 0.001
```

Crypto backtests use Backtesting.py's fractional engine:

```powershell
trading-pipeline --backtest --symbol "BTC/USD" --asset-class crypto --timeframe 1Hour --limit 500 --cash 10000 --commission 0.001
```

The backtest uses Backtesting.py and runs the same SMA/RSI/candlestick signal
logic as the alert pipeline. It is long-only: `BUY` opens a position and `SELL`
closes an open position.

Indicators and candlestick patterns are computed through a small feature
framework backed by pandas-ta-classic. Backtests precompute the full feature
frame once, then each Backtesting.py step reads the current feature row instead
of recalculating indicators for every candle.

Telegram alerts are optional:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
trading-pipeline --symbol AAPL --telegram
```

Dashboard:

```powershell
trading-dashboard --server.port 8501
```

The dashboard reads the SQLite database configured by `DATABASE_PATH` and
includes overview metrics, signal history, candlestick charts with indicators,
backtest run/trade tables, equity curves, drawdown charts, trade PnL, and
trade entry/exit overlays.

Run the test suite:

```powershell
python -m pytest -q
```

## Current strategy

The initial strategy is deliberately simple:

- SMA 20 / SMA 50 crossover
- RSI 14 filter
- pandas-ta-classic candlestick confirmation across the available candle
  pattern set
- additional feature columns are already computed for MACD, Bollinger Bands,
  and ATR for future strategy/risk work

This is a starting point for pipeline validation, not a production-grade trading
strategy.

## Storage

The CLI initializes SQLite automatically and stores:

- `market_bars`: OHLCV bars by symbol, asset class, timeframe, timestamp
- `feature_snapshots`: normalized indicator values and detected patterns
- `signals`: alert decisions with reason, feature snapshot, and optional order id
- `backtest_runs`: summary metrics and full stats JSON
- `backtest_trades`: Backtesting.py trade rows linked to a backtest run
- `backtest_equity_curve`: equity and drawdown rows linked to a backtest run

The `data/` directory is ignored by git.
