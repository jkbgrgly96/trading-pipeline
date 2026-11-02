from datetime import UTC, datetime, timedelta

from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.dashboard import queries
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshots_from_frame
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import History, SmaRsiStrategy


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
    signal = SmaRsiStrategy().signal("TEST", AssetClass.STOCK, History(snapshots_from_frame(frame)))
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
    equity_curve = queries.load_backtest_equity_curve(db_path, int(runs.iloc[0]["id"]))

    assert len(bars) == 90
    assert len(features) == 90
    assert len(runs) == 1
    assert len(trades) >= 0
    assert not equity_curve.empty
    assert {"timestamp", "equity", "drawdown_pct"}.issubset(equity_curve.columns)


def _simulate_alerts(db_path):
    from fakes import FakeBroker, RecordingNotifier, after_close, crash_bar, crossover_bars
    from trading_pipeline.pipeline import run_once

    storage = Storage(db_path)
    storage.initialize()
    bars = crossover_bars()
    broker = FakeBroker(bars)
    for _ in range(2):
        run_once(
            broker, RecordingNotifier(), "TEST", AssetClass.STOCK, "1Day", 500, False, 100,
            storage, now=after_close(bars),
        )
        bars.append(crash_bar(bars[-1]))
    storage.record_run("TEST", AssetClass.STOCK, "1Day", bars[-1].timestamp, "boom")
    return storage


def test_load_decisions_positions_and_status(tmp_path):
    db_path = tmp_path / "alerts.sqlite3"
    _simulate_alerts(db_path)

    decisions = queries.load_decisions(db_path)
    positions = queries.load_positions(db_path)
    status = queries.load_symbol_status(db_path)
    overview = queries.load_overview(db_path)

    assert list(decisions["decision"]) == ["exit_long", "enter_long"]
    assert set(decisions["notification"]) == {"sent"}
    assert len(positions) == 1 and positions.iloc[0]["status"] == "closed"
    assert positions.iloc[0]["pnl_pct"] < 0
    assert status.iloc[0]["health"] == "error"
    assert overview["open_positions"] == 0
    assert overview["latest_decision"]["decision"] == "exit_long"


def test_market_chart_draws_decisions_and_position_levels(tmp_path):
    from trading_pipeline.dashboard.charts import build_market_chart

    db_path = tmp_path / "alerts.sqlite3"
    _simulate_alerts(db_path)
    figure = build_market_chart(
        bars=queries.load_market_bars(db_path, "TEST", "stock", "1Day"),
        features=queries.load_feature_snapshots(db_path, "TEST", "stock", "1Day"),
        signals=queries.load_signals(db_path, "TEST", "stock", "1Day"),
        positions=queries.load_positions(db_path, symbol="TEST"),
    )
    names = {trace.name for trace in figure.data}
    assert {"ENTER (alert)", "EXIT (alert)", "Entry", "Stop", "Target"}.issubset(names)
