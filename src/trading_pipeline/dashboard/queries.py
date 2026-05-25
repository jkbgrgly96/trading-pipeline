from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd


FEATURE_COLUMNS = [
    "sma_20",
    "sma_50",
    "rsi_14",
    "macd",
    "macd_hist",
    "macd_signal",
    "bbands_20_2_lower",
    "bbands_20_2_middle",
    "bbands_20_2_upper",
    "bbands_20_2_bandwidth",
    "bbands_20_2_percent",
    "atr_14",
]


def connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def load_symbols(database_path: str | Path) -> pd.DataFrame:
    return _read_sql(
        database_path,
        """
        SELECT DISTINCT symbol, asset_class, timeframe
        FROM market_bars
        UNION
        SELECT DISTINCT symbol, asset_class, timeframe
        FROM signals
        UNION
        SELECT DISTINCT symbol, asset_class, timeframe
        FROM backtest_runs
        ORDER BY symbol, asset_class, timeframe
        """,
    )


def load_overview(database_path: str | Path) -> dict[str, Any]:
    with connect(database_path) as connection:
        return {
            "table_counts": _table_counts(connection),
            "latest_signal": _one_row(connection, "SELECT * FROM signals ORDER BY created_at DESC LIMIT 1"),
            "latest_backtest": _one_row(connection, "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 1"),
            "last_market_bar": _one_row(connection, "SELECT * FROM market_bars ORDER BY timestamp DESC LIMIT 1"),
            "signal_action_counts": _read_sql_from_connection(
                connection,
                "SELECT action, COUNT(*) AS count FROM signals GROUP BY action ORDER BY action",
            ),
        }


def load_signals(
    database_path: str | Path,
    symbol: str | None = None,
    asset_class: str | None = None,
    timeframe: str | None = None,
    action: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="bar_timestamp",
        start=start,
        end=end,
    )
    if action:
        clauses.append("action = ?")
        params.append(action)

    query = "SELECT * FROM signals"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY bar_timestamp DESC, created_at DESC"
    return _flatten_signal_frame(_read_sql(database_path, query, params))


def load_market_bars(
    database_path: str | Path,
    symbol: str,
    asset_class: str,
    timeframe: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="timestamp",
        start=start,
        end=end,
    )
    query = "SELECT * FROM market_bars"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY timestamp ASC"
    frame = _read_sql(database_path, query, params)
    return _parse_datetime_columns(frame, ["timestamp", "created_at"])


def load_feature_snapshots(
    database_path: str | Path,
    symbol: str,
    asset_class: str,
    timeframe: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="timestamp",
        start=start,
        end=end,
    )
    query = "SELECT * FROM feature_snapshots"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY timestamp ASC"
    return _flatten_feature_frame(_read_sql(database_path, query, params))


def load_backtest_runs(
    database_path: str | Path,
    symbol: str | None = None,
    asset_class: str | None = None,
    timeframe: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="created_at",
        start=start,
        end=end,
    )
    query = "SELECT * FROM backtest_runs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC"
    frame = _read_sql(database_path, query, params)
    return _parse_datetime_columns(frame, ["created_at", "start", "end"])


def load_backtest_trades(database_path: str | Path, backtest_run_id: int) -> pd.DataFrame:
    frame = _read_sql(
        database_path,
        """
        SELECT *
        FROM backtest_trades
        WHERE backtest_run_id = ?
        ORDER BY entry_time ASC
        """,
        [backtest_run_id],
    )
    return _parse_datetime_columns(frame, ["entry_time", "exit_time"])


def load_backtest_equity_curve(
    database_path: str | Path,
    backtest_run_id: int,
) -> pd.DataFrame:
    frame = _read_sql(
        database_path,
        """
        SELECT *
        FROM backtest_equity_curve
        WHERE backtest_run_id = ?
        ORDER BY timestamp ASC
        """,
        [backtest_run_id],
    )
    return _parse_datetime_columns(frame, ["timestamp"])


def _filter_clauses(
    symbol: str | None,
    asset_class: str | None,
    timeframe: str | None,
    timestamp_column: str,
    start: str | None,
    end: str | None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in [
        ("symbol", symbol),
        ("asset_class", asset_class),
        ("timeframe", timeframe),
    ]:
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if start:
        clauses.append(f"{timestamp_column} >= ?")
        params.append(start)
    if end:
        clauses.append(f"{timestamp_column} <= ?")
        params.append(end)
    return clauses, params


def _read_sql(
    database_path: str | Path,
    query: str,
    params: list[Any] | None = None,
) -> pd.DataFrame:
    with connect(database_path) as connection:
        return _read_sql_from_connection(connection, query, params)


def _read_sql_from_connection(
    connection: sqlite3.Connection,
    query: str,
    params: list[Any] | None = None,
) -> pd.DataFrame:
    return pd.read_sql_query(query, connection, params=params or [])


def _one_row(connection: sqlite3.Connection, query: str) -> dict[str, Any] | None:
    row = connection.execute(query).fetchone()
    return dict(row) if row else None


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "market_bars",
        "feature_snapshots",
        "signals",
        "backtest_runs",
        "backtest_trades",
        "backtest_equity_curve",
    ]
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _flatten_signal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = _parse_datetime_columns(frame, ["created_at", "bar_timestamp"])
    frame["features"] = frame["features_json"].apply(_parse_json)
    frame["patterns"] = frame["patterns_json"].apply(_parse_json)
    frame["patterns_text"] = frame["patterns"].apply(_join_patterns)
    return _add_feature_columns(frame)


def _flatten_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = _parse_datetime_columns(frame, ["timestamp", "created_at"])
    frame["features"] = frame["features_json"].apply(_parse_json)
    frame["patterns"] = frame["patterns_json"].apply(_parse_json)
    frame["patterns_text"] = frame["patterns"].apply(_join_patterns)
    return _add_feature_columns(frame)


def _add_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    for column in FEATURE_COLUMNS:
        frame[column] = frame["features"].apply(
            lambda features, key=column: features.get(key) if isinstance(features, dict) else None
        )
    return frame


def _parse_datetime_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


def _parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _join_patterns(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
