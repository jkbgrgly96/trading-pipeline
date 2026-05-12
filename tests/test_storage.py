from datetime import UTC, datetime, timedelta

from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshot_from_frame
from trading_pipeline.storage import Storage
from trading_pipeline.strategies.sma_rsi import generate_signal_from_features


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
    signal = generate_signal_from_features(
        "TEST",
        AssetClass.STOCK,
        latest_snapshot(frame),
        snapshot_from_frame(frame, -2),
    )

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
