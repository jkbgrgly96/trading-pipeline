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

Telegram alerts are optional:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
trading-pipeline --symbol AAPL --telegram
```

## Current strategy

The initial strategy is deliberately simple:

- SMA 20 / SMA 50 crossover
- RSI 14 filter
- Basic candlestick confirmation:
  - bullish engulfing
  - bearish engulfing
  - hammer
  - shooting star

This is a starting point for pipeline validation, not a production-grade trading
strategy.
