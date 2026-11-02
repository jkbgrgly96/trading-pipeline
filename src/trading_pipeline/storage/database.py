from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from trading_pipeline.domain import (
    AssetClass,
    Bar,
    Decision,
    OpenPosition,
    Signal,
)
from trading_pipeline.features import FeatureSnapshot, snapshots_from_frame

if TYPE_CHECKING:
    from trading_pipeline.backtest.runner import BacktestConfig, BacktestResult

SCHEMA_VERSION = 7


class Storage:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    # --- connection / schema --------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, roll back on error, always close.

        ``with sqlite3.connect(...)`` alone only manages the transaction and
        leaves the connection open, which leaks handles and can keep the DB
        file locked on Windows.
        """
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create or migrate the schema.

        Runs under ``BEGIN IMMEDIATE`` so two processes starting at once (e.g.
        the watcher and the dashboard) cannot both run a migration.
        """
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        try:
            # WAL lets the dashboard read while the watcher writes. It must be
            # set outside a transaction.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 1:
                    _execute_script(connection, SCHEMA_V1)
                if version < 2:
                    _migrate_to_v2(connection)
                if version < 3:
                    _migrate_to_v3(connection)
                if version < 4:
                    _migrate_to_v4(connection)
                if version < 5:
                    _migrate_to_v5(connection)
                if version < 6:
                    _migrate_to_v6(connection)
                if version < 7:
                    _migrate_to_v7(connection)
                if version < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    # --- market data ----------------------------------------------------

    def save_market_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        bars: list[Bar],
        source: str = "alpaca",
    ) -> None:
        now = _now()
        rows = [
            (
                symbol,
                asset_class.value,
                timeframe,
                iso_timestamp(bar.timestamp),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                source,
                now,
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
        now = _now()
        rows = [
            (
                symbol,
                asset_class.value,
                timeframe,
                iso_timestamp(snapshot.timestamp),
                _json(snapshot.values),
                _json(snapshot.patterns),
                now,
            )
            for snapshot in snapshots_from_frame(feature_frame)
        ]
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

    # --- signals / decisions --------------------------------------------

    def signal_exists(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        bar_timestamp: datetime,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM signals
                WHERE symbol = ? AND asset_class = ? AND timeframe = ? AND bar_timestamp = ?
                """,
                (symbol, asset_class.value, timeframe, iso_timestamp(bar_timestamp)),
            ).fetchone()
        return row is not None

    def save_signal(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        signal: Signal,
        snapshot: FeatureSnapshot,
        orders_enabled: bool,
        order_id: str | None = None,
        decision: Decision | None = None,
        notify_required: bool = False,
        notify_message: str | None = None,
        strategy: str | None = None,
        strategy_params: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Insert the signal/decision for a bar.

        Returns the new row id, or ``None`` if this bar was already processed
        (the unique key makes reruns idempotent: no duplicate alerts/orders).
        """
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signals (
                    created_at, symbol, asset_class, timeframe, bar_timestamp,
                    action, confidence, latest_close, reason, patterns_json,
                    features_json, order_id, orders_enabled, decision,
                    decision_reason, stop_loss, take_profit, notify_required,
                    notify_message, strategy, strategy_params
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe, bar_timestamp) DO NOTHING
                """,
                (
                    _now(),
                    symbol,
                    asset_class.value,
                    timeframe,
                    iso_timestamp(signal.timestamp),
                    signal.action.value,
                    signal.confidence,
                    signal.latest_close,
                    signal.reason,
                    _json(snapshot.patterns),
                    _json(snapshot.values),
                    order_id,
                    int(orders_enabled),
                    decision.action.value if decision else None,
                    decision.reason if decision else None,
                    decision.stop_loss if decision else signal.stop_loss,
                    decision.take_profit if decision else signal.take_profit,
                    int(notify_required),
                    notify_message,
                    strategy,
                    _optional_json(strategy_params),
                ),
            )
            if cursor.rowcount == 0:
                return None
            return int(cursor.lastrowid)

    def update_signal_order(
        self,
        signal_id: int,
        order_id: str | None,
        order_status: str,
        order_error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE signals SET order_id = ?, order_status = ?, order_error = ?
                WHERE id = ?
                """,
                (order_id, order_status, order_error, signal_id),
            )

    def update_signal_order_id(self, signal_id: int, order_id: str) -> None:
        self.update_signal_order(signal_id, order_id, "submitted")

    def set_notify_message(self, signal_id: int, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE signals SET notify_message = ? WHERE id = ?",
                (message, signal_id),
            )

    def set_signal_position(self, signal_id: int, position_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE signals SET position_id = ? WHERE id = ?",
                (position_id, signal_id),
            )

    def mark_notified(self, signal_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE signals
                SET notified_at = ?, notify_error = NULL, notify_attempts = notify_attempts + 1
                WHERE id = ?
                """,
                (_now(), signal_id),
            )

    def mark_notify_failed(self, signal_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE signals
                SET notify_error = ?, notify_attempts = notify_attempts + 1
                WHERE id = ?
                """,
                (error[:500], signal_id),
            )

    def pending_notifications(self) -> list[dict[str, Any]]:
        """Decisions that should have been sent but were not yet (outbox)."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM signals
                WHERE notify_required = 1 AND notified_at IS NULL
                  AND notify_abandoned_at IS NULL
                ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notify_abandoned(self, signal_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE signals SET notify_abandoned_at = ? WHERE id = ?",
                (_now(), signal_id),
            )

    def last_processed_bar(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
    ) -> datetime | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(bar_timestamp) FROM signals
                WHERE symbol = ? AND asset_class = ? AND timeframe = ?
                """,
                (symbol, asset_class.value, timeframe),
            ).fetchone()
        return pd.Timestamp(row[0]).to_pydatetime() if row and row[0] else None

    def recent_exit_order_ids(self, symbol: str, asset_class: AssetClass, limit: int = 5) -> list[str]:
        """Exit orders of the most recently closed positions for ``symbol``."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT exit_order_id FROM positions
                WHERE symbol = ? AND asset_class = ? AND status = 'closed'
                  AND exit_order_id IS NOT NULL
                ORDER BY updated_at DESC LIMIT ?
                """,
                (symbol, asset_class.value, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None

    # --- positions ------------------------------------------------------

    def get_open_position(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
    ) -> OpenPosition | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM positions
                WHERE symbol = ? AND asset_class = ? AND timeframe = ? AND status = 'open'
                """,
                (symbol, asset_class.value, timeframe),
            ).fetchone()
        return _row_to_position(row) if row else None

    def open_position(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        entry_price: float,
        entry_time: datetime,
        stop_loss: float | None,
        take_profit: float | None,
        source: str,
        quantity: float | None = None,
        entry_signal_id: int | None = None,
        entry_order_id: str | None = None,
        strategy: str | None = None,
        strategy_params: Mapping[str, Any] | None = None,
        notional: float | None = None,
    ) -> int:
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO positions (
                    symbol, asset_class, timeframe, source, status, entry_time,
                    entry_price, quantity, stop_loss, take_profit, entry_signal_id,
                    entry_order_id, created_at, updated_at, strategy, strategy_params, notional
                )
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    asset_class.value,
                    timeframe,
                    source,
                    iso_timestamp(entry_time),
                    entry_price,
                    quantity,
                    stop_loss,
                    take_profit,
                    entry_signal_id,
                    entry_order_id,
                    now,
                    now,
                    strategy,
                    _optional_json(strategy_params),
                    notional,
                ),
            )
            return int(cursor.lastrowid)

    def count_open_positions(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()[0]
            )

    def positions_awaiting_fills(self, since: datetime) -> list[dict[str, Any]]:
        """Positions with an entry or exit order whose fill is not recorded yet.

        Only positions opened (entry) or last changed (exit) after ``since``,
        so orders that never fill are not polled forever.
        """
        cutoff = iso_timestamp(since)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM positions
                WHERE (entry_order_id IS NOT NULL AND entry_fill_price IS NULL AND created_at >= ?)
                   OR (exit_order_id IS NOT NULL AND exit_fill_price IS NULL AND updated_at >= ?)
                ORDER BY id
                """,
                (cutoff, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_entry_fill(
        self, position_id: int, price: float, quantity: float, filled_at: datetime | None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE positions
                SET entry_fill_price = ?, quantity = ?, entry_filled_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (price, quantity, _optional_timestamp(filled_at), _now(), position_id),
            )

    def record_exit_fill(self, position_id: int, price: float, filled_at: datetime | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE positions SET exit_fill_price = ?, exit_filled_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (price, _optional_timestamp(filled_at), _now(), position_id),
            )

    def update_position_stop(self, position_id: int, stop_loss: float) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE positions SET stop_loss = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                (stop_loss, _now(), position_id),
            )

    def set_position_entry_order(self, position_id: int, order_id: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE positions SET entry_order_id = ?, updated_at = ? WHERE id = ?",
                (order_id, _now(), position_id),
            )

    def close_position(
        self,
        position_id: int,
        exit_price: float | None,
        exit_time: datetime,
        exit_reason: str,
        exit_signal_id: int | None = None,
        exit_order_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE positions
                SET status = 'closed', exit_price = ?, exit_time = ?, exit_reason = ?,
                    exit_signal_id = ?, exit_order_id = ?, updated_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (
                    exit_price,
                    iso_timestamp(exit_time),
                    exit_reason,
                    exit_signal_id,
                    exit_order_id,
                    _now(),
                    position_id,
                ),
            )

    # --- watcher health -------------------------------------------------

    def record_run(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        last_bar_timestamp: datetime | None,
        error: str | None = None,
    ) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO symbol_status (
                    symbol, asset_class, timeframe, last_run_at, last_success_at,
                    last_bar_timestamp, last_error, last_error_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_success_at = COALESCE(excluded.last_success_at, last_success_at),
                    last_bar_timestamp = COALESCE(excluded.last_bar_timestamp, last_bar_timestamp),
                    last_error = excluded.last_error,
                    last_error_at = COALESCE(excluded.last_error_at, last_error_at)
                """,
                (
                    symbol,
                    asset_class.value,
                    timeframe,
                    now,
                    None if error else now,
                    iso_timestamp(last_bar_timestamp) if last_bar_timestamp else None,
                    error[:500] if error else None,
                    now if error else None,
                ),
            )

    # --- backtests ------------------------------------------------------

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
                    trade_count, win_rate_pct, stats_json, strategy, sizing, notional,
                    strategy_params, risk_per_trade, max_notional
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    # With notional sizing: the share of starting cash each entry uses.
                    config.position_size
                    if config.position_size is not None
                    else config.notional / config.cash,
                    _to_storage_value(summary.get("Start")),
                    _to_storage_value(summary.get("End")),
                    _to_storage_value(summary.get("Return [%]")),
                    _to_storage_value(summary.get("Buy & Hold Return [%]")),
                    _to_storage_value(summary.get("Max. Drawdown [%]")),
                    _to_storage_value(summary.get("Sharpe Ratio")),
                    _to_storage_value(summary.get("# Trades")),
                    _to_storage_value(summary.get("Win Rate [%]")),
                    _json(summary),
                    config.strategy,
                    config.sizing,
                    config.notional if config.position_size is None else None,
                    _json(result.strategy_params),
                    config.risk_per_trade if config.sizing == "risk" else None,
                    config.max_notional if config.sizing == "risk" else None,
                ),
            )
            run_id = int(cursor.lastrowid)
            self._save_backtest_trades(connection, run_id, symbol, result.trades)
            self._save_backtest_equity_curve(connection, run_id, result.equity_curve)
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

    def _save_backtest_equity_curve(
        self,
        connection: sqlite3.Connection,
        backtest_run_id: int,
        equity_curve: pd.DataFrame,
    ) -> None:
        if equity_curve.empty:
            return

        curve = equity_curve.copy()
        equity_column = _find_first_column(curve, ["Equity", "equity"])
        drawdown_column = _find_first_column(
            curve,
            ["DrawdownPct", "Drawdown [%]", "Drawdown", "drawdown_pct"],
            required=False,
        )

        equity = pd.to_numeric(curve[equity_column], errors="coerce")
        if drawdown_column:
            drawdown_pct = pd.to_numeric(curve[drawdown_column], errors="coerce")
            if drawdown_column == "Drawdown":
                drawdown_pct = drawdown_pct * 100
        else:
            peak = equity.cummax()
            drawdown_pct = ((equity / peak) - 1) * 100

        rows = []
        for (timestamp, row), equity_value, drawdown_value in zip(
            curve.iterrows(),
            equity,
            drawdown_pct,
            strict=True,
        ):
            rows.append(
                (
                    backtest_run_id,
                    _to_storage_value(timestamp),
                    _to_storage_value(equity_value),
                    _to_storage_value(drawdown_value),
                    _json(row.to_dict()),
                )
            )

        connection.executemany(
            """
            INSERT INTO backtest_equity_curve (
                backtest_run_id, timestamp, equity, drawdown_pct, raw_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )


def iso_timestamp(value: datetime | pd.Timestamp) -> str:
    """Canonical text form for stored timestamps (UTC when tz-aware)."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def _row_to_position(row: sqlite3.Row) -> OpenPosition:
    return OpenPosition(
        id=int(row["id"]),
        symbol=row["symbol"],
        asset_class=AssetClass(row["asset_class"]),
        timeframe=row["timeframe"],
        entry_price=float(row["entry_price"]),
        entry_time=pd.Timestamp(row["entry_time"]).to_pydatetime(),
        quantity=row["quantity"],
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        source=row["source"],
        entry_order_id=row["entry_order_id"],
        entry_fill_price=row["entry_fill_price"],
    )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    """Rebuild ``signals`` with decision/outbox columns and a per-bar unique key.

    Pre-existing duplicate rows for the same bar are collapsed to the most
    recent one. Old rows are marked as not needing notification so upgrading
    never re-sends historical alerts.
    """
    _execute_script(
        connection,
        "ALTER TABLE signals RENAME TO signals_v1;"
        + SIGNALS_V2
        + """
        INSERT INTO signals (
            id, created_at, symbol, asset_class, timeframe, bar_timestamp, action,
            confidence, latest_close, reason, patterns_json, features_json,
            order_id, orders_enabled, notify_required
        )
        SELECT id, created_at, symbol, asset_class, timeframe, bar_timestamp, action,
               confidence, latest_close, reason, patterns_json, features_json,
               order_id, orders_enabled, 0
        FROM signals_v1
        WHERE id IN (
            SELECT MAX(id) FROM signals_v1
            GROUP BY symbol, asset_class, timeframe, bar_timestamp
        );
        DROP TABLE signals_v1;
        """
        + POSITIONS_AND_STATUS,
    )


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    """Record which strategy produced each signal, position and backtest.

    Everything stored before v3 came from the only strategy that existed,
    ``sma_rsi``. Notification columns are untouched, so nothing is re-sent.
    """
    for table in ("signals", "positions", "backtest_runs"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN strategy TEXT")
        connection.execute(f"UPDATE {table} SET strategy = 'sma_rsi'")


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    """Record how backtest entries were sized.

    ``sizing`` is ``notional`` (fixed USD per entry, like paper orders; the
    amount is in ``notional``) or ``equity_fraction`` (``position_size`` of
    current equity). Every run before v4 used ``equity_fraction``.
    """
    connection.execute("ALTER TABLE backtest_runs ADD COLUMN sizing TEXT")
    connection.execute("ALTER TABLE backtest_runs ADD COLUMN notional REAL")
    connection.execute("UPDATE backtest_runs SET sizing = 'equity_fraction'")


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    """Paper fill prices and times, next to the alert prices (``entry_price`` /
    ``exit_price`` stay the bar close the decision was made on)."""
    for column in ("entry_fill_price REAL", "entry_filled_at TEXT", "exit_fill_price REAL", "exit_filled_at TEXT"):
        connection.execute(f"ALTER TABLE positions ADD COLUMN {column}")


def _migrate_to_v6(connection: sqlite3.Connection) -> None:
    """Strategy parameters (JSON, every parameter including defaults).

    Earlier rows ran with the defaults of their strategy; they stay NULL.
    """
    for table in ("signals", "positions", "backtest_runs"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN strategy_params TEXT")


def _migrate_to_v7(connection: sqlite3.Connection) -> None:
    """Entry sizing: ``positions.notional`` is the USD amount an entry buys (or,
    alerts only, the suggested amount); backtests record their risk sizing."""
    connection.execute("ALTER TABLE positions ADD COLUMN notional REAL")
    connection.execute("ALTER TABLE backtest_runs ADD COLUMN risk_per_trade REAL")
    connection.execute("ALTER TABLE backtest_runs ADD COLUMN max_notional REAL")


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Run statements one by one inside the caller's transaction.

    ``executescript`` would COMMIT first and drop the migration lock. The
    schema scripts contain no semicolons inside literals or triggers.
    """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _backtest_summary(stats: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _to_storage_value(value)
        for key, value in stats.items()
        if not key.startswith("_")
    }


def _find_first_column(
    frame: pd.DataFrame,
    candidates: list[str],
    required: bool = True,
) -> str | None:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    if required:
        raise KeyError(f"None of the columns exist: {candidates}")
    return None


def _optional_json(value: Any) -> str | None:
    return None if value is None else _json(value)


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), sort_keys=True)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if _is_missing(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _to_storage_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    # Array-likes return an array; only scalars can be "missing".
    return isinstance(result, bool | np.bool_) and bool(result)


def _optional_timestamp(value: datetime | None) -> str | None:
    return iso_timestamp(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA_V1 = """
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

CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    equity REAL,
    drawdown_pct REAL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_curve_lookup
ON backtest_equity_curve(backtest_run_id, timestamp);
"""

SIGNALS_V2 = """
CREATE TABLE signals (
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
    orders_enabled INTEGER NOT NULL,
    decision TEXT,
    decision_reason TEXT,
    stop_loss REAL,
    take_profit REAL,
    position_id INTEGER,
    order_status TEXT,
    order_error TEXT,
    notify_required INTEGER NOT NULL DEFAULT 0,
    notify_message TEXT,
    notified_at TEXT,
    notify_abandoned_at TEXT,
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    notify_error TEXT,
    UNIQUE(symbol, asset_class, timeframe, bar_timestamp)
);

CREATE INDEX idx_signals_decision ON signals(decision, bar_timestamp);
CREATE INDEX idx_signals_outbox ON signals(notify_required, notified_at);
"""

POSITIONS_AND_STATUS = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL,
    stop_loss REAL,
    take_profit REAL,
    entry_signal_id INTEGER,
    entry_order_id TEXT,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_signal_id INTEGER,
    exit_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- At most one open position per strategy instance.
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_open
ON positions(symbol, asset_class, timeframe) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS symbol_status (
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    last_run_at TEXT NOT NULL,
    last_success_at TEXT,
    last_bar_timestamp TEXT,
    last_error TEXT,
    last_error_at TEXT,
    PRIMARY KEY (symbol, asset_class, timeframe)
);
"""
