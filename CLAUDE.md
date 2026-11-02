# CLAUDE.md

Guidance for Claude (and other coding agents) working in this repository.

## What this is

Alert-first trading pipeline. Its main job: **notify the user when a position
should be opened or closed.** It pulls closed OHLCV bars from Alpaca, computes
indicators/candle patterns, turns the strategy signal plus the current position
into a decision (enter / exit / none), alerts via console/Telegram, optionally
mirrors the decision on the Alpaca **paper** account, and persists everything
to SQLite. There is a Backtesting.py backtest using the same decision code and
a Streamlit dashboard.

## Commands

```bash
uv sync --extra dev                      # install
uv run python -m pytest -q               # all tests (fast, offline)
uv run ruff check src tests              # lint (CI runs both)
uv run trading-pipeline --once           # one pass over watchlist.toml
uv run trading-pipeline --watch          # continuous alerts
uv run trading-pipeline --backtest ...   # backtest
uv run trading-dashboard                 # Streamlit dashboard
```

Tests never hit the network. Use `tests/fakes.py` (`FakeBroker`,
`RecordingNotifier`, `crossover_bars()` = real data that produces a BUY on bar
97) instead of mocks or real credentials.

## Flow (per symbol, per closed bar)

`watch.Watcher.run_cycle` → `flush_outbox` → `pipeline.run_once` per watchlist item:
1. `broker.get_bars` → `timeframes.closed_bars` (drop the forming bar) → need ≥ `strategy.min_bars`.
2. `_pending_indexes`: every closed bar after `storage.last_processed_bar` (first run: latest bar
   only; catch-up capped at `MAX_CATCH_UP_BARS`). Nothing pending → `already_processed`.
3. Features (`strategy.feature_definitions()`) computed once over all closed bars; then for each pending
   bar, oldest first (`_process_bar`): `_current_position` (paper reconciliation) →
   `strategy.signal(History)` → `decide` →
   `save_signal` (unique per bar, stores the message = outbox) → `_apply_decision` → `deliver`.
4. Undelivered alerts are retried by `flush_outbox` for `OUTBOX_MAX_AGE`, then reported as dropped.

## Layout (`src/trading_pipeline/`)

| Module | Role |
|---|---|
| `domain.py` | Frozen dataclasses/enums: `Bar`, `Signal`, `Decision`, `OpenPosition`, `SignalAction`, `DecisionAction`, `ExitReason` |
| `timeframes.py` | Parse `5Min`/`1Hour`/`1Day`, Alpaca mapping, lookback windows, `closed_bars` |
| `config.py` | `Settings` from env + `.env` loader; `orders_enabled` safety gate |
| `brokers/base.py`, `brokers/alpaca.py` | `Broker` ABC (latest bars, bars in a date range, position, buy, close, order status) and the paper-only Alpaca adapter (split-adjusted stock bars via `ALPACA_ADJUSTMENT`) |
| `features/` | `IndicatorDefinition` list → `compute_feature_frame` (pandas-ta-classic) → `FeatureSnapshot`; `snapshots_from_frame` for bulk |
| `strategies/` | `base.py`: `Strategy` ABC (`features`, `min_bars`, `signal`, `risk_levels`) and `History` (look-ahead-free view); `__init__.py`: `STRATEGIES` registry, `get_strategy`; `sma_rsi.py`: `SmaRsiStrategy` (default) |
| `decisions.py` | `decide()`: the single source of truth for entries/exits (live + backtest) |
| `pipeline.py` | `run_once`, `deliver`, `flush_outbox`, position reconciliation, order application |
| `watch.py` | Watchlist TOML loading/validation, `Watcher` loop, error alerts with cooldown |
| `notifications/` | `Notifier.send_message`, `MultiNotifier`, console, Telegram, `formatting.format_decision` |
| `backtest/runner.py` | `fetch_backtest_bars` (latest `limit` or a date range + warm-up), Backtesting.py strategy wrapping `BacktestConfig.strategy` + `decide()` with notional or equity-fraction sizing; `default_costs` |
| `storage/database.py` | SQLite schema, versioned migrations (`PRAGMA user_version`), all writes |
| `dashboard/` | Streamlit app (`app.py`), read-only SQL (`queries.py`), Plotly figures (`charts.py`) |

## Conventions

- Python ≥ 3.11, `from __future__ import annotations` in every module; ruff config in `pyproject.toml`.
- Domain objects are frozen dataclasses. Enums subclass `(str, Enum)` (UP042 is ignored on purpose).
- Feature frame columns: OHLCV capitalised (`Open`…`Volume`, required by Backtesting.py);
  indicators use `IndicatorDefinition.name`; candle patterns are `pattern__<name>` and become
  `bullish_<name>` / `bearish_<name>` in snapshots.
- Strategies read features only through `History` / `FeatureSnapshot.get(name)`, never the frame.
  A strategy lists the indicators it needs in `features`; `DEFAULT_FEATURES` are always computed
  because the dashboard chart draws them. Same name with different settings is an error.
- New strategy: frozen dataclass subclassing `Strategy` (parameters as fields, stateless), registered
  in `strategies.STRATEGIES`, with tests. It only returns signals; never put position or order logic
  in a strategy.
- **Never fork entry/exit logic between live and backtest.** Change `decisions.decide` (and its
  tests) instead. Exits are evaluated on the closed bar's high/low, matching what an alert can do.
- Timestamps are stored as ISO-8601 TEXT in UTC via `storage.database.iso_timestamp`. Use it for
  every write and lookup, or idempotency checks will silently miss.
- Use `Storage.connect()` / `queries.connect()` as context managers: they commit and **close**.
- Schema changes: bump `SCHEMA_VERSION` and add a `_migrate_to_vN` step. Never edit an existing
  table definition in place; existing DB files won't get it. Migrations must not re-trigger
  notifications for historical rows.
- Heavy optional imports (`alpaca`, `backtesting`, `streamlit`) stay lazy inside functions.

## Safety rules (do not weaken)

- Orders only when `ALPACA_PAPER=true`, `TRADING_MODE=paper`, `PLACE_ORDERS=true`
  (`Settings.orders_enabled`). Never add an order path outside that gate or default any of them on.
- `AlpacaBroker` never creates a `TradingClient` unless `paper=True`, and every trading call goes
  through `Broker.ensure_paper()`. Keep both layers.
- Never place real orders, including in manual testing. There is no live-trading mode; don't add
  one unless explicitly asked.
- Never hard-code or commit credentials. `.env` and `watchlist.toml` are git-ignored.

## Known limitations

- Virtual positions assume the user acts on every alert at the bar close.
- Stock daily bars are stamped at midnight ET, so a daily bar counts as closed (and alerts) from
  midnight ET; the matching order would execute at the next open, as in the backtest.
- Notional stock orders are DAY market orders; submitted after hours they queue for the next open.
- Stop/target are checked on bar close (alert semantics), not as resting broker orders. They are
  never checked against the bar a position was opened or adopted on.
- Signals stored before schema v2 have no virtual positions, so their exits are not tracked.
- Backtests run each symbol on its own (own cash); nothing portfolio-level is simulated. Backtest
  fills use the bar's prices, so slippage beyond `--spread` is not modelled.
- Stock bars are split-adjusted, but a position held through a split keeps its pre-split entry,
  stop and target, so the first post-split bar looks like a stop-loss. Close such positions by hand.
- One strategy per symbol/asset class/timeframe (signals and positions are keyed by those).
  Changing an entry's strategy hands its open position to the new one, with the stored stop/target.
