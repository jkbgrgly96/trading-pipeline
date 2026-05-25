from datetime import UTC, datetime, timedelta

from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.dashboard import queries
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshot_from_frame
from trading_pipeline.storage import Storage
from trading_pipeline.strategies.sma_rsi import generate_signal_from_features


def make_bars(count: int = 90) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(
            timestamp=start + timedelta(days=index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100 + index,
            volume=1_000,
        )
        for index in range(count)
    ]


def seed_database(path):
    storage = Storage(path)
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
    storage.save_signal(
        "TEST",
        AssetClass.STOCK,
        "1Day",
        signal,
        latest_snapshot(frame),
        orders_enabled=False,
    )
    result = run_strategy_backtest(
        bars,
        BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK),
    )
    storage.save_backtest(
        "TEST",
        AssetClass.STOCK,
        "1Day",
        len(bars),
        BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK),
        result,
    )
    return storage


def test_load_symbols_and_overview(tmp_path):
    db_path = tmp_path / "dashboard.sqlite3"
    seed_database(db_path)

    symbols = queries.load_symbols(db_path)
    overview = queries.load_overview(db_path)

    assert symbols.iloc[0]["symbol"] == "TEST"
    assert overview["table_counts"]["market_bars"] == 90
    assert overview["latest_signal"]["symbol"] == "TEST"


def test_empty_database_queries_return_empty_frames_and_counts(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    storage = Storage(db_path)
    storage.initialize()

    overview = queries.load_overview(db_path)

    assert queries.load_symbols(db_path).empty
    assert queries.load_signals(db_path).empty
    assert queries.load_backtest_runs(db_path).empty
    assert overview["table_counts"]["signals"] == 0
    assert overview["latest_signal"] is None


def test_load_signals_flattens_features(tmp_path):
    db_path = tmp_path / "dashboard.sqlite3"
    seed_database(db_path)

    signals = queries.load_signals(db_path, symbol="TEST")

    assert len(signals) == 1
    assert "rsi_14" in signals.columns
    assert "patterns_text" in signals.columns


def test_load_signals_filters_by_action_and_date(tmp_path):
    db_path = tmp_path / "dashboard.sqlite3"
    seed_database(db_path)

    all_signals = queries.load_signals(db_path, symbol="TEST")
    latest = all_signals.iloc[0]
    filtered = queries.load_signals(
        db_path,
        symbol="TEST",
        action=latest["action"],
        start=latest["bar_timestamp"].date().isoformat(),
        end=(latest["bar_timestamp"] + timedelta(days=1)).date().isoformat(),
    )

    assert len(filtered) == 1
    assert filtered.iloc[0]["id"] == latest["id"]


def test_load_bars_features_and_backtests(tmp_path):
    db_path = tmp_path / "dashboard.sqlite3"
    seed_database(db_path)

    bars = queries.load_market_bars(db_path, "TEST", "stock", "1Day")
    features = queries.load_feature_snapshots(db_path, "TEST", "stock", "1Day")
    runs = queries.load_backtest_runs(db_path, symbol="TEST")
    trades = queries.load_backtest_trades(db_path, int(runs.iloc[0]["id"]))

    assert len(bars) == 90
    assert len(features) == 90
    assert len(runs) == 1
    assert len(trades) >= 0
