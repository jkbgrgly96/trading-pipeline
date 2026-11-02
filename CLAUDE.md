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
uv run trading-pipeline --sweep --strategy NAME ...  # parameter search (in/out-of-sample)
uv run trading-dashboard                 # Streamlit dashboard
```

Tests never hit the network. Use `tests/fakes.py` (`FakeBroker`,
`RecordingNotifier`, `crossover_bars()` = real data that produces a BUY on bar
97) instead of mocks or real credentials.

## Flow (per symbol, per closed bar)

`watch.Watcher.run_cycle` → `flush_outbox` → `sync_order_fills` (paper orders on) → `pipeline.run_once` per
watchlist item (strategy built from the item's name + params; benchmark bars cached per cycle):
1. `broker.get_bars` → `timeframes.closed_bars` (drop the forming bar) → need ≥ `strategy.min_bars`.
2. `_pending_indexes`: every closed bar after `storage.last_processed_bar` (first run: latest bar
   only; catch-up capped at `MAX_CATCH_UP_BARS`). Nothing pending → `already_processed`.
3. `strategies.strategy_frame` (features + `strategy.derive`, with benchmark closes when
   `benchmark_for` names one) computed once over all closed bars; then for each pending bar, oldest
   first (`_process_bar`): `_current_position` (paper reconciliation) → `decisions.evaluate`
   (`strategy.signal` + `strategy.manage` → `decide`; `entries_allowed=False` once
   `max_open_positions` are open) → `save_signal` (unique per bar, stores the message = outbox) →
   `_apply_decision` (entry sized by `sizing.Sizing`; a raised trailing stop is stored) → `deliver`.
4. Undelivered alerts are retried by `flush_outbox` for `OUTBOX_MAX_AGE`, then reported as dropped.
5. `sync_order_fills` records paper fill prices (`entry_fill_price`/`exit_fill_price`) of orders placed
   within `FILL_SYNC_WINDOW`; `entry_price`/`exit_price` stay the alert's bar close.

## Layout (`src/trading_pipeline/`)

| Module | Role |
|---|---|
| `domain.py` | Frozen dataclasses/enums: `Bar`, `Signal`, `Decision`, `OpenPosition`, `PositionPlan`, `SignalAction`, `DecisionAction`, `ExitReason`, `StopTrigger` |
| `timeframes.py` | Parse `5Min`/`1Hour`/`1Day`, Alpaca mapping, lookback windows, `closed_bars` |
| `config.py` | `Settings` from env + `.env` loader; `orders_enabled` safety gate |
| `brokers/base.py`, `brokers/alpaca.py` | `Broker` ABC (latest bars, bars in a date range, position, buy, close, `get_order` → `BrokerOrder` with fills) and the paper-only Alpaca adapter (split-adjusted stock bars via `ALPACA_ADJUSTMENT`) |
| `features/` | `IndicatorDefinition` list → `compute_feature_frame` (pandas-ta-classic) → `FeatureSnapshot`; `snapshots_from_frame` for bulk |
| `strategies/` | `base.py`: `Strategy` ABC (`features`, `min_bars`, `signal`, `risk_levels`; optional `derive`, `benchmark_for`, `manage`, `stop_trigger`), `History` (look-ahead-free view, `since`), `strategy_frame`; `__init__.py`: `STRATEGIES` registry, `get_strategy(name, params)` with type coercion; `sma_rsi.py` (default), `indicathor.py`, `rsi2_pullback.py`, `donchian_breakout.py` |
| `decisions.py` | `evaluate()` (signal + position plan → decision) and `decide()`: the single source of truth for entries/exits, stop triggers and trailing stops (live + backtest) |
| `sizing.py` | `Sizing`: notional or risk-based entry size (risk to the stop, capped); used by paper orders, alerts and backtests |
| `pipeline.py` | `run_once`, `deliver`, `flush_outbox`, position reconciliation, order application |
| `watch.py` | Watchlist TOML loading/validation, `Watcher` loop, error alerts with cooldown |
| `notifications/` | `Notifier.send_message`, `MultiNotifier`, console, Telegram, `formatting.format_decision` |
| `backtest/runner.py` | `fetch_backtest_bars` (latest `limit` or a date range + warm-up), Backtesting.py strategy wrapping `BacktestConfig.strategy` + `decisions.evaluate()` with notional, risk or equity-fraction sizing; `default_costs` |
| `backtest/sweep.py` | Parameter sweep: `DEFAULT_GRIDS`, `parse_grid`, `run_sweep` (in-sample ranking by total P/L, top configs + defaults + `sma_rsi` re-run out of sample) |
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
- New strategy: frozen dataclass subclassing `Strategy` (parameters as `int`/`float`/`bool`/`str`
  fields, validated in `__post_init__`, stateless), registered in `strategies.STRATEGIES`, with tests.
  It returns signals and, via `manage`, a `PositionPlan` (stop raise / exit request); it never
  places orders or changes positions itself. `derive` columns must be causal
  (`test_every_registered_strategy_is_causal`). Indicators come from pandas-ta-classic (add a kind
  to `features/backends/pandas_ta.py`), not hand-written formulas.
- **Never fork entry/exit logic between live and backtest.** Change `decisions.decide` (and its
  tests) instead. Exits are evaluated on the closed bar's high/low (or its close with
  `stop_trigger = "close"`), matching what an alert can do; a raised stop applies from the next bar.
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
- Backtests run each symbol on its own (own cash); nothing portfolio-level is simulated, including
  `MAX_OPEN_POSITIONS`. Backtest fills use the bar's prices, so slippage beyond `--spread` is not
  modelled; compare with the paper fill-vs-alert columns.
- Stop/target are set from the alert price, not the paper fill (same as the backtest).
- With the SIP feed (default), stock bars are only available 15 minutes after they close (free
  plan), so intraday stock alerts arrive ~16 minutes after the bar boundary.
- Stock bars are split-adjusted, but a position held through a split keeps its pre-split entry,
  stop and target, so the first post-split bar looks like a stop-loss. Close such positions by hand.
- One strategy per symbol/asset class/timeframe (signals and positions are keyed by those).
  Changing an entry's strategy hands its open position to the new one, with the stored stop/target.
