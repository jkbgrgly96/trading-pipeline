# CLAUDE.md

Guidance for Claude (and other coding agents) working in this repository.

## What this is

Alert-first trading pipeline. Pulls OHLCV bars from Alpaca, computes indicators
and candlestick patterns, produces a BUY/SELL/HOLD signal, notifies (console or
Telegram), optionally places Alpaca **paper** orders, persists everything to
SQLite, and has a Backtesting.py backtest plus a Streamlit dashboard.

## Commands

The project uses `uv` (there is a `uv.lock`); plain `pip install -e .[dev]` also works.

```bash
uv sync --extra dev                 # install
uv run python -m pytest -q          # run all tests (fast, no network)
uv run trading-pipeline --help      # CLI: live alert pass or --backtest
uv run trading-dashboard            # Streamlit dashboard (wraps `streamlit run`)
```

Tests never hit the network. Anything touching Alpaca or Telegram must be
tested with fakes (subclass `Broker` / `Notifier`), never real credentials.

## Layout (`src/trading_pipeline/`)

| Module | Role |
|---|---|
| `domain.py` | Frozen dataclasses: `Bar`, `Signal`; enums `AssetClass`, `SignalAction` |
| `config.py` | `Settings` from env + a minimal `.env` loader; `orders_enabled` safety gate |
| `brokers/base.py`, `brokers/alpaca.py` | `Broker` ABC (`get_bars`, `submit_signal_order`) and the Alpaca adapter |
| `features/` | Feature framework: `IndicatorDefinition` list → `compute_feature_frame` (pandas-ta-classic backend) → `FeatureSnapshot` |
| `strategies/sma_rsi.py` | The only strategy: SMA20/50 cross + RSI filter + candle-pattern confidence bump |
| `pipeline.py` | `run_once`: fetch → features → signal → store → notify → (maybe) order |
| `backtest/runner.py` | Wraps the same strategy in a Backtesting.py `Strategy` (long-only) |
| `storage/database.py` | SQLite schema (`SCHEMA` string) and `Storage` write methods |
| `dashboard/` | Streamlit app (`app.py`), read-only SQL (`queries.py`), Plotly figures (`charts.py`) |
| `notifications/` | `ConsoleNotifier`, `TelegramNotifier` (stdlib `urllib`) |
| `candles.py` | Convenience wrapper for pattern detection only |

## Conventions

- Python ≥ 3.11, `from __future__ import annotations` at the top of every module.
- Domain objects are `@dataclass(frozen=True)`. Keep them immutable.
- Feature frame columns: OHLCV are capitalised (`Open`, `High`, `Low`, `Close`,
  `Volume`) because Backtesting.py requires it. Indicator columns use the
  `IndicatorDefinition.name` (e.g. `sma_20`, `macd_hist`, `bbands_20_2_upper`).
  Candle patterns are `pattern__<name>`; a snapshot turns non-zero values into
  `bullish_<name>` / `bearish_<name>`.
- The strategy reads features only through `FeatureSnapshot`; add a property
  there when a strategy needs a new feature, and add the definition to
  `DEFAULT_FEATURES`.
- Live and backtest must share `generate_signal_from_features`. Do not fork
  signal logic between them.
- Timestamps are stored as ISO-8601 TEXT in UTC. SQL date filters compare
  strings, so keep that format consistent.
- Schema changes: `SCHEMA` uses `CREATE TABLE IF NOT EXISTS`; there are no
  migrations, so altering an existing table won't reach existing DB files.
  Add a migration step if you change columns.
- Optional heavy imports (`alpaca`, `backtesting`) are imported lazily inside
  functions. Keep that pattern so tests run without network clients.

## Safety rules (do not weaken)

- Orders are submitted only when `ALPACA_PAPER=true`, `TRADING_MODE=paper`,
  and `PLACE_ORDERS=true` (`Settings.orders_enabled`). Never add a code path
  that submits orders outside this gate, and never default any of them on.
- Never hard-code or commit credentials. `.env` is git-ignored; keep it that way.
- Don't add live-money (non-paper) trading without an explicit request.

## Known gaps (see code review, 2026-10-31)

- Live SELL path submits a notional market sell without checking for an open
  position; backtest is long-only. These diverge.
- No idempotency: re-running on the same bar re-alerts and can re-order.
- Live orders ignore `stop_loss` / `take_profit`; the backtest uses them.
- `pipeline.run_once`, the strategy rules, `config`, and the Alpaca/Telegram
  adapters have no tests.
