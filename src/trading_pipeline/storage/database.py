from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trading_pipeline.backtest.runner import BacktestConfig, BacktestResult
from trading_pipeline.domain import AssetClass, Bar, Signal
from trading_pipeline.features import FeatureSnapshot, snapshot_from_frame


class Storage:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def save_market_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        bars: list[Bar],
        source: str = "alpaca",
    ) -> None:
        rows = [
            (
                symbol,
                asset_class.value,
                timeframe,
                bar.timestamp.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                source,
                _now(),
            )
            for bar in bars
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_bars (
                    symbol, asset_class, timeframe, timestamp, open, high, low,
                    close, volume, source, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe, timestamp, source)
                DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                rows,
            )

    def save_feature_snapshots(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        feature_frame: pd.DataFrame,
    ) -> None:
        rows = []
        for index in range(len(feature_frame)):
            snapshot = snapshot_from_frame(feature_frame, index)
            rows.append(
                (
                    symbol,
                    asset_class.value,
                    timeframe,
                    snapshot.timestamp.isoformat(),
                    _json(snapshot.values),
                    _json(snapshot.patterns),
                    _now(),
                )
            )

        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO feature_snapshots (
                    symbol, asset_class, timeframe, timestamp, features_json,
                    patterns_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe, timestamp)
                DO UPDATE SET
                    features_json = excluded.features_json,
                    patterns_json = excluded.patterns_json
                """,
                rows,
            )

    def save_signal(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        signal: Signal,
        snapshot: FeatureSnapshot,
        orders_enabled: bool,
        order_id: str | None = None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signals (
                    created_at, symbol, asset_class, timeframe, bar_timestamp,
                    action, confidence, latest_close, reason, patterns_json,
                    features_json, order_id, orders_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    symbol,
                    asset_class.value,
                    timeframe,
                    signal.timestamp.isoformat(),
                    signal.action.value,
                    signal.confidence,
                    signal.latest_close,
                    signal.reason,
                    _json(snapshot.patterns),
                    _json(snapshot.values),
                    order_id,
                    int(orders_enabled),
                ),
            )
            return int(cursor.lastrowid)

    def update_signal_order_id(self, signal_id: int, order_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE signals SET order_id = ? WHERE id = ?",
                (order_id, signal_id),
            )

    def save_backtest(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        bar_limit: int,
        config: BacktestConfig,
        result: BacktestResult,
    ) -> int:
        summary = _backtest_summary(result.stats)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backtest_runs (
                    created_at, symbol, asset_class, timeframe, bar_limit, cash,
                    commission, spread, position_size, start, end, return_pct,
                    buy_hold_return_pct, max_drawdown_pct, sharpe_ratio,
                    trade_count, win_rate_pct, stats_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    symbol,
                    asset_class.value,
                    timeframe,
                    bar_limit,
                    config.cash,
                    config.commission,
                    config.spread,
                    config.position_size,
                    _to_storage_value(summary.get("Start")),
                    _to_storage_value(summary.get("End")),
                    _to_storage_value(summary.get("Return [%]")),
                    _to_storage_value(summary.get("Buy & Hold Return [%]")),
                    _to_storage_value(summary.get("Max. Drawdown [%]")),
                    _to_storage_value(summary.get("Sharpe Ratio")),
                    _to_storage_value(summary.get("# Trades")),
                    _to_storage_value(summary.get("Win Rate [%]")),
                    _json(summary),
                ),
            )
            run_id = int(cursor.lastrowid)
            self._save_backtest_trades(connection, run_id, symbol, result.trades)
            return run_id

    def _save_backtest_trades(
        self,
        connection: sqlite3.Connection,
        backtest_run_id: int,
        symbol: str,
        trades: pd.DataFrame,
    ) -> None:
        rows = []
        for _, row in trades.iterrows():
            rows.append(
                (
                    backtest_run_id,
                    symbol,
                    _to_storage_value(row.get("EntryTime")),
                    _to_storage_value(row.get("ExitTime")),
                    _to_storage_value(row.get("EntryPrice")),
                    _to_storage_value(row.get("ExitPrice")),
                    _to_storage_value(row.get("Size")),
                    _to_storage_value(row.get("PnL")),
                    _to_storage_value(row.get("ReturnPct")),
                    _to_storage_value(row.get("Duration")),
                    _to_storage_value(row.get("Tag")),
                    _json(row.to_dict()),
                )
            )
        connection.executemany(
            """
            INSERT INTO backtest_trades (
                backtest_run_id, symbol, entry_time, exit_time, entry_price,
                exit_price, size, pnl, return_pct, duration, tag, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _backtest_summary(stats: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _to_storage_value(value) for key, value in stats.items() if not key.startswith("_")}


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), sort_keys=True)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _to_storage_value(value: Any) -> Any:
    if value is None:
        return None
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS market_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, asset_class, timeframe, timestamp, source)
);

CREATE INDEX IF NOT EXISTS idx_market_bars_lookup
ON market_bars(symbol, asset_class, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    features_json TEXT NOT NULL,
    patterns_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, asset_class, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_feature_snapshots_lookup
ON feature_snapshots(symbol, asset_class, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    latest_close REAL NOT NULL,
    reason TEXT NOT NULL,
    patterns_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    order_id TEXT,
    orders_enabled INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_lookup
ON signals(symbol, asset_class, timeframe, bar_timestamp);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_limit INTEGER NOT NULL,
    cash REAL NOT NULL,
    commission REAL NOT NULL,
    spread REAL NOT NULL,
    position_size REAL NOT NULL,
    start TEXT,
    end TEXT,
    return_pct REAL,
    buy_hold_return_pct REAL,
    max_drawdown_pct REAL,
    sharpe_ratio REAL,
    trade_count INTEGER,
    win_rate_pct REAL,
    stats_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_lookup
ON backtest_runs(symbol, asset_class, timeframe, created_at);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    return_pct REAL,
    duration TEXT,
    tag TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);
"""
