from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fakes import FakeBroker, after_close, crossover_bars
from trading_pipeline.backtest.runner import (
    BacktestConfig,
    _first_tradable_index,
    bars_to_ohlcv_frame,
    fetch_backtest_bars,
    run_strategy_backtest,
    summarize_stats,
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


def test_backtest_config_defaults_to_paper_order_sizing():
    config = BacktestConfig(
        symbol="AAPL",
        asset_class=AssetClass.STOCK,
    )

    assert config.sizing == "notional"
    assert config.notional == 100.0
    assert config.position_size is None
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


# --- sizing, trading window, data fetching ---------------------------------

def _run(bars, **overrides):
    pytest.importorskip("backtesting")
    config = BacktestConfig(
        symbol="TEST", asset_class=AssetClass.STOCK, trade_on_close=True, **overrides
    )
    return run_strategy_backtest(bars, config)


def test_notional_sizing_buys_the_paper_order_amount():
    result = _run(crossover_bars(149), notional=250.0)

    assert len(result.trades) >= 1
    cost = result.trades["Size"] * result.trades["EntryPrice"]
    assert cost.tolist() == pytest.approx([250.0] * len(cost), abs=0.01)
    assert summarize_stats(result.stats)["P/L [$]"] == pytest.approx(result.trades["PnL"].sum())


def test_equity_fraction_sizing_uses_current_equity():
    result = _run(crossover_bars(149), position_size=0.5)

    first = result.trades.iloc[0]
    assert first["Size"] * first["EntryPrice"] == pytest.approx(5_000, rel=0.01)


def test_trading_starts_at_start_and_warm_up_bars_are_not_in_the_stats():
    bars = crossover_bars(149)
    start = bars[100].timestamp  # after the bar-97 BUY: it must not be taken

    result = _run(bars, start=start)

    assert all(entry >= start for entry in result.trades["EntryTime"])
    assert result.stats["Start"] == bars[99].timestamp  # one bar of context for Backtesting.py
    assert len(result.equity_curve) == len(bars) - 99


def test_without_start_the_first_tradable_bar_is_after_indicator_warm_up():
    result = _run(crossover_bars(149))

    assert result.stats["Start"] == crossover_bars(149)[58].timestamp  # min_bars 60 -> bar 59 trades
    assert result.trades.iloc[0]["EntryTime"] == crossover_bars(149)[97].timestamp


def test_start_after_the_data_is_rejected():
    bars = crossover_bars(80)
    index = pd.DatetimeIndex([bar.timestamp for bar in bars])
    with pytest.raises(ValueError, match="No bars to trade"):
        _first_tradable_index(index, 60, bars[-1].timestamp + timedelta(days=5))


def test_fetch_with_start_requests_a_range_with_indicator_warm_up():
    bars = crossover_bars(149)
    broker = FakeBroker(bars)
    start, end = bars[100].timestamp, bars[140].timestamp
    now = after_close(bars)

    fetched = fetch_backtest_bars(
        broker, "TEST", AssetClass.STOCK, "1Day", min_bars=60, limit=10, start=start, end=end, now=now
    )

    (_, requested_start, requested_end), = broker.range_requests
    assert requested_end == end
    assert sum(1 for bar in bars if requested_start <= bar.timestamp < start) >= 60
    assert fetched[-1].timestamp == bars[139].timestamp


def test_fetch_without_start_uses_the_latest_limit_bars_and_drops_the_forming_one():
    bars = crossover_bars(149)
    now = bars[-1].timestamp + timedelta(hours=3)  # last bar still forming

    fetched = fetch_backtest_bars(FakeBroker(bars), "TEST", AssetClass.STOCK, "1Day", 60, limit=100, now=now)

    assert len(fetched) == 99
    assert fetched[-1].timestamp == bars[-2].timestamp
