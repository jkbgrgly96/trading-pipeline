from datetime import UTC, datetime, timedelta

import pytest

from trading_pipeline.backtest.runner import (
    BacktestConfig,
    bars_to_ohlcv_frame,
    run_strategy_backtest,
)
from trading_pipeline.domain import AssetClass, Bar


def test_bars_to_ohlcv_frame_uses_backtesting_column_names():
    bars = [
        Bar(
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            open=1,
            high=2,
            low=0.5,
            close=1.5,
            volume=100,
        ),
        Bar(
            timestamp=datetime(2026, 1, 2, tzinfo=UTC),
            open=1.5,
            high=2.5,
            low=1,
            close=2,
            volume=150,
        ),
    ]

    frame = bars_to_ohlcv_frame(bars)

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.iloc[-1]["Close"] == 2


def test_backtest_config_defaults_to_long_only_size():
    config = BacktestConfig(
        symbol="AAPL",
        asset_class=AssetClass.STOCK,
    )

    assert config.position_size == 0.95
    assert config.cash == 10_000.0


def test_run_strategy_backtest_returns_stats_when_engine_is_installed():
    pytest.importorskip("backtesting")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[Bar] = []

    for index in range(90):
        close = 100 + index * 0.2
        bars.append(
            Bar(
                timestamp=start + timedelta(days=index),
                open=close - 0.1,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1_000,
            )
        )

    result = run_strategy_backtest(
        bars,
        BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK),
    )

    assert "Return [%]" in result.stats
    assert "# Trades" in result.stats
