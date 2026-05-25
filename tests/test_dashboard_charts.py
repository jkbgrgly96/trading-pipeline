from datetime import UTC, datetime, timedelta

from trading_pipeline.dashboard.charts import build_market_chart, build_trade_pnl_chart
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


def test_build_market_chart_returns_plotly_figure(tmp_path):
    db_path = tmp_path / "chart.sqlite3"
    storage = Storage(db_path)
    storage.initialize()
    bars = make_bars()
    feature_frame = compute_feature_frame(bars)
    signal = generate_signal_from_features(
        "TEST",
        AssetClass.STOCK,
        latest_snapshot(feature_frame),
        snapshot_from_frame(feature_frame, -2),
    )
    storage.save_market_bars("TEST", AssetClass.STOCK, "1Day", bars)
    storage.save_feature_snapshots("TEST", AssetClass.STOCK, "1Day", feature_frame)
    storage.save_signal(
        "TEST",
        AssetClass.STOCK,
        "1Day",
        signal,
        latest_snapshot(feature_frame),
        orders_enabled=False,
    )

    figure = build_market_chart(
        bars=queries.load_market_bars(db_path, "TEST", "stock", "1Day"),
        features=queries.load_feature_snapshots(db_path, "TEST", "stock", "1Day"),
        signals=queries.load_signals(db_path, "TEST", "stock", "1Day"),
    )

    assert figure.data
    assert any(trace.name == "OHLC" for trace in figure.data)
    assert any(trace.name == "RSI 14" for trace in figure.data)


def test_build_market_chart_handles_empty_inputs():
    import pandas as pd

    figure = build_market_chart(
        bars=pd.DataFrame(),
        features=pd.DataFrame(),
        signals=pd.DataFrame(),
    )

    assert figure.data == ()


def test_build_trade_pnl_chart_returns_bar_trace():
    import pandas as pd

    trades = pd.DataFrame(
        {
            "exit_time": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "pnl": [10.0, -5.0],
        }
    )

    figure = build_trade_pnl_chart(trades)

    assert len(figure.data) == 1
    assert figure.data[0].name == "PnL"
