from datetime import UTC, datetime, timedelta

from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshots_from_frame
from trading_pipeline.storage import Storage
from trading_pipeline.storage.database import SCHEMA_VERSION
from trading_pipeline.strategies import History, SmaRsiStrategy


def make_bars(count: int = 90) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(
            timestamp=start + timedelta(days=index),
            open=100 + index * 0.2,
            high=101 + index * 0.2,
            low=99 + index * 0.2,
            close=100 + index * 0.2,
            volume=1_000,
        )
        for index in range(count)
    ]


def test_storage_persists_bars_features_and_signal(tmp_path):
    storage = Storage(tmp_path / "pipeline.sqlite3")
    storage.initialize()
    bars = make_bars()
    frame = compute_feature_frame(bars)
    signal = SmaRsiStrategy().signal("TEST", AssetClass.STOCK, History(snapshots_from_frame(frame)))

    storage.save_market_bars("TEST", AssetClass.STOCK, "1Day", bars)
    storage.save_feature_snapshots("TEST", AssetClass.STOCK, "1Day", frame)
    signal_id = storage.save_signal(
        "TEST",
        AssetClass.STOCK,
        "1Day",
        signal,
        latest_snapshot(frame),
        orders_enabled=False,
    )

    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0] == 90
        assert connection.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0] == 90
        row = connection.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        assert row["symbol"] == "TEST"
        assert row["action"] == signal.action.value


def test_storage_persists_backtest_run_and_trades(tmp_path):
    storage = Storage(tmp_path / "pipeline.sqlite3")
    storage.initialize()
    result = run_strategy_backtest(
        make_bars(),
        BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK),
    )

    run_id = storage.save_backtest(
        symbol="TEST",
        asset_class=AssetClass.STOCK,
        timeframe="1Day",
        bar_limit=90,
        config=BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK),
        result=result,
    )

    with storage.connect() as connection:
        run = connection.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
        assert run["symbol"] == "TEST"
        assert run["bar_limit"] == 90
        assert connection.execute(
            "SELECT COUNT(*) FROM backtest_trades WHERE backtest_run_id = ?",
            (run_id,),
        ).fetchone()[0] == len(result.trades)
        assert connection.execute(
            "SELECT COUNT(*) FROM backtest_equity_curve WHERE backtest_run_id = ?",
            (run_id,),
        ).fetchone()[0] == len(result.equity_curve)


def test_initialize_migrates_v1_database_and_dedupes_signals(tmp_path):
    import sqlite3

    from trading_pipeline.storage.database import SCHEMA_V1

    path = tmp_path / "old.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(SCHEMA_V1)
    for action in ["buy", "buy", "sell"]:
        legacy.execute(
            """
            INSERT INTO signals (created_at, symbol, asset_class, timeframe, bar_timestamp,
                action, confidence, latest_close, reason, patterns_json, features_json,
                orders_enabled)
            VALUES ('t', 'AAPL', 'stock', '1Day', '2026-01-02T05:00:00+00:00', ?, 0.6, 1,
                'r', '[]', '{}', 0)
            """,
            (action,),
        )
    legacy.commit()
    legacy.close()

    storage = Storage(path)
    storage.initialize()
    storage.initialize()  # idempotent

    with storage.connect() as connection:
        rows = connection.execute("SELECT action, notify_required, strategy FROM signals").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert [tuple(row) for row in rows] == [("sell", 0, "sma_rsi")]
    assert version == SCHEMA_VERSION
    assert storage.pending_notifications() == []


def test_v2_to_v3_records_sma_rsi_without_resending_alerts(tmp_path):
    import sqlite3

    from trading_pipeline.storage.database import SCHEMA_V1, _execute_script, _migrate_to_v2

    path = tmp_path / "v2.sqlite3"
    legacy = sqlite3.connect(path)
    _execute_script(legacy, SCHEMA_V1)
    _migrate_to_v2(legacy)
    legacy.execute("PRAGMA user_version = 2")
    legacy.execute(
        """
        INSERT INTO signals (created_at, symbol, asset_class, timeframe, bar_timestamp, action,
            confidence, latest_close, reason, patterns_json, features_json, orders_enabled,
            decision, notify_required, notified_at)
        VALUES ('t', 'AAPL', 'stock', '1Day', '2026-01-02T05:00:00+00:00', 'buy', 0.6, 1,
            'r', '[]', '{}', 0, 'enter_long', 1, 't')
        """
    )
    legacy.execute(
        """
        INSERT INTO positions (symbol, asset_class, timeframe, source, status, entry_time,
            entry_price, created_at, updated_at)
        VALUES ('AAPL', 'stock', '1Day', 'virtual', 'open', '2026-01-02T05:00:00+00:00', 1, 't', 't')
        """
    )
    legacy.commit()
    legacy.close()

    storage = Storage(path)
    storage.initialize()

    with storage.connect() as connection:
        signal = connection.execute("SELECT strategy, notified_at FROM signals").fetchone()
        position = connection.execute("SELECT strategy FROM positions").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert tuple(signal) == ("sma_rsi", "t")
    assert position["strategy"] == "sma_rsi"
    assert version == 3
    assert storage.pending_notifications() == []
    assert storage.get_open_position("AAPL", AssetClass.STOCK, "1Day") is not None


def test_duplicate_signal_for_same_bar_is_rejected(tmp_path):
    storage = Storage(tmp_path / "pipeline.sqlite3")
    storage.initialize()
    frame = compute_feature_frame(make_bars())
    latest = latest_snapshot(frame)
    signal = SmaRsiStrategy().signal("TEST", AssetClass.STOCK, History(snapshots_from_frame(frame)))
    args = ("TEST", AssetClass.STOCK, "1Day", signal, latest, False)
    assert storage.save_signal(*args) is not None
    assert storage.save_signal(*args) is None
