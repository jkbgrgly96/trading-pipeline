from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from trading_pipeline.dashboard.formatting import parse_json
from trading_pipeline.timeframes import parse_timeframe

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


ACTIONABLE_DECISIONS = ("enter_long", "exit_long")


@contextmanager
def connect(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Read connection that is always closed (the watcher may be writing)."""
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


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
        UNION
        SELECT DISTINCT symbol, asset_class, timeframe
        FROM positions
        ORDER BY symbol, asset_class, timeframe
        """,
    )


def load_overview(database_path: str | Path) -> dict[str, Any]:
    with connect(database_path) as connection:
        return {
            "table_counts": _table_counts(connection),
            "latest_signal": _one_row(connection, "SELECT * FROM signals ORDER BY created_at DESC LIMIT 1"),
            "latest_decision": _one_row(
                connection,
                "SELECT * FROM signals WHERE decision IN ('enter_long', 'exit_long') "
                "ORDER BY bar_timestamp DESC, id DESC LIMIT 1",
            ),
            "open_positions": int(
                connection.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
            ),
            "pending_notifications": int(
                connection.execute(
                    "SELECT COUNT(*) FROM signals WHERE notify_required = 1 AND notified_at IS NULL "
                    "AND notify_abandoned_at IS NULL"
                ).fetchone()[0]
            ),
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


def load_decisions(
    database_path: str | Path,
    symbol: str | None = None,
    asset_class: str | None = None,
    timeframe: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Actionable decisions (entries/exits) with notification and order status."""
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="bar_timestamp",
        start=start,
        end=end,
    )
    clauses.append("decision IN (?, ?)")
    params.extend(ACTIONABLE_DECISIONS)
    query = (
        "SELECT * FROM signals WHERE "
        + " AND ".join(clauses)
        + " ORDER BY bar_timestamp DESC, id DESC"
    )
    frame = _flatten_signal_frame(_read_sql(database_path, query, params))
    if frame.empty:
        return frame
    frame["notification"] = frame.apply(_notification_state, axis=1)
    return frame


def load_positions(
    database_path: str | Path,
    status: str | None = None,
    symbol: str | None = None,
    asset_class: str | None = None,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Positions joined with the latest stored close for unrealized P/L.

    P/L uses the paper fill prices once they are known, else the alert prices
    (the bar close each decision was made on). ``*_fill_vs_alert_pct`` show
    the difference: positive on entry and negative on exit both cost money.
    """
    clauses, params = _filter_clauses(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        timestamp_column="entry_time",
        start=None,
        end=None,
        table_alias="p",
    )
    if status:
        clauses.append("p.status = ?")
        params.append(status)
    query = """
        SELECT p.*,
            (SELECT close FROM market_bars b
             WHERE b.symbol = p.symbol AND b.asset_class = p.asset_class
               AND b.timeframe = p.timeframe
             ORDER BY b.timestamp DESC LIMIT 1) AS last_close,
            (SELECT timestamp FROM market_bars b
             WHERE b.symbol = p.symbol AND b.asset_class = p.asset_class
               AND b.timeframe = p.timeframe
             ORDER BY b.timestamp DESC LIMIT 1) AS last_bar_timestamp
        FROM positions p
    """
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY p.status DESC, p.entry_time DESC"
    frame = _parse_datetime_columns(
        _read_sql(database_path, query, params),
        ["entry_time", "exit_time", "last_bar_timestamp", "created_at", "updated_at"],
    )
    if frame.empty:
        return frame
    entry_alert = _numeric(frame, "entry_price")
    exit_alert = _numeric(frame, "exit_price")
    entry_fill = _numeric(frame, "entry_fill_price")
    exit_fill = _numeric(frame, "exit_fill_price")
    entry = entry_fill.fillna(entry_alert)
    closed = frame["status"] == "closed"
    frame["mark_price"] = exit_fill.fillna(exit_alert).where(closed, _numeric(frame, "last_close"))
    frame["pnl_pct"] = (frame["mark_price"] / entry - 1) * 100
    frame["entry_fill_vs_alert_pct"] = (entry_fill / entry_alert - 1) * 100
    frame["exit_fill_vs_alert_pct"] = (exit_fill / exit_alert - 1) * 100
    frame["to_stop_pct"] = (pd.to_numeric(frame["stop_loss"], errors="coerce") / frame["mark_price"] - 1) * 100
    frame["to_target_pct"] = (
        pd.to_numeric(frame["take_profit"], errors="coerce") / frame["mark_price"] - 1
    ) * 100
    return frame


def load_symbol_status(database_path: str | Path, now: datetime | None = None) -> pd.DataFrame:
    """Watcher health per symbol: last run, last processed bar, errors, staleness."""
    frame = _read_sql(database_path, "SELECT * FROM symbol_status ORDER BY symbol, timeframe")
    frame = _parse_datetime_columns(
        frame, ["last_run_at", "last_success_at", "last_bar_timestamp", "last_error_at"]
    )
    if frame.empty:
        return frame
    now = now or datetime.now(UTC)
    frame["health"] = frame.apply(lambda row: _health(row, now), axis=1)
    return frame


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
    table_alias: str | None = None,
) -> tuple[list[str], list[Any]]:
    prefix = f"{table_alias}." if table_alias else ""
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in [
        ("symbol", symbol),
        ("asset_class", asset_class),
        ("timeframe", timeframe),
    ]:
        if value:
            clauses.append(f"{prefix}{column} = ?")
            params.append(value)
    if start:
        clauses.append(f"{prefix}{timestamp_column} >= ?")
        params.append(start)
    if end:
        clauses.append(f"{prefix}{timestamp_column} <= ?")
        params.append(end)
    return clauses, params


def _notification_state(row: pd.Series) -> str:
    if pd.notna(row.get("notified_at")):
        return "sent"
    if pd.notna(row.get("notify_abandoned_at")):
        return "dropped (undeliverable)"
    if row.get("notify_required") in (1, True):
        return f"failed ({int(row.get('notify_attempts') or 0)} tries)"
    return "not required"


def _health(row: pd.Series, now: datetime) -> str:
    """"error" if the last run failed, "stale" if runs or bars stopped arriving."""
    if pd.notna(row.get("last_error")) and row.get("last_error"):
        return "error"
    last_run = row.get("last_run_at")
    if pd.isna(last_run):
        return "never_ran"
    try:
        duration = parse_timeframe(str(row["timeframe"])).duration
    except ValueError:
        duration = timedelta(hours=1)
    run_budget = max(timedelta(minutes=30), 2 * min(duration, timedelta(minutes=15)))
    if now - last_run.to_pydatetime() > run_budget:
        return "stale (watcher not running?)"
    return "ok"


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
        "positions",
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
    frame["features"] = frame["features_json"].apply(parse_json)
    frame["patterns"] = frame["patterns_json"].apply(parse_json)
    frame["patterns_text"] = frame["patterns"].apply(_join_patterns)
    return _add_feature_columns(frame)


def _flatten_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = _parse_datetime_columns(frame, ["timestamp", "created_at"])
    frame["features"] = frame["features_json"].apply(parse_json)
    frame["patterns"] = frame["patterns_json"].apply(parse_json)
    frame["patterns_text"] = frame["patterns"].apply(_join_patterns)
    return _add_feature_columns(frame)


def _add_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    for column in FEATURE_COLUMNS:
        frame[column] = frame["features"].apply(
            lambda features, key=column: features.get(key) if isinstance(features, dict) else None
        )
    return frame


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    """Column as floats; all-NaN when it does not exist (database not migrated yet)."""
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce")


def _parse_datetime_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    for column in columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True)
    return frame


def _join_patterns(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
