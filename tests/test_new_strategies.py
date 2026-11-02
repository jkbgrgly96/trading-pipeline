from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, random_walk_bars
from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.domain import AssetClass, OpenPosition, SignalAction
from trading_pipeline.features import FeatureSnapshot
from trading_pipeline.pipeline import run_once
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import (
    DonchianBreakoutStrategy,
    History,
    IndicathorStrategy,
    MarketContext,
    Rsi2PullbackStrategy,
    get_strategy,
)
from trading_pipeline.strategies.indicathor import (
    MACD_FLAG,
    RELATIVE_STRENGTH,
    RSI_FLAG,
    SCORE_EMA,
    _macd_cross_flag,
    _macd_state_flag,
    _rsi_flag,
)

TS = datetime(2026, 3, 2, tzinfo=UTC)
NAN = float("nan")


def snap(day=0, close=100.0, high=None, low=None, **values) -> FeatureSnapshot:
    return FeatureSnapshot(
        TS + timedelta(days=day), close, high if high is not None else close + 1,
        low if low is not None else close - 1, close, 1_000, values, [],
    )


def series(values) -> pd.Series:
    return pd.Series(values, dtype=float)


# --- indicathor: flags ---------------------------------------------------------------------


def test_rsi_flag_levels():
    flags = _rsi_flag(series([25, 30, 40, 50, 55, 70, 80, NAN]))
    assert flags.iloc[:7].tolist() == [-2, -2, -1, 0, 1, 2, 2]
    assert np.isnan(flags.iloc[7])


def test_macd_state_flag():
    macd = series([2, -1, 1, -2, NAN])
    signal = series([1, -2, 2, -1, 0])
    flags = _macd_state_flag(macd, signal)
    assert flags.iloc[:4].tolist() == [2, 1, -1, -2]
    assert np.isnan(flags.iloc[4])


def test_macd_cross_flag_reproduces_the_event_logic():
    # cross up below zero -> +1 (held), cross down above zero -> -1, cross up above zero -> 0
    macd = series([-3, -1, -0.5, 1, 2, 1, 2.5, 3])
    signal = series([-2, -2, -1.0, 0, 1, 2, 2.0, 2])
    assert _macd_cross_flag(macd, signal).tolist() == [0, 1, 1, 1, 1, -1, 0, 0]


# --- indicathor: rules -----------------------------------------------------------------------


def ithor_snap(day=1, close=100.0, score=1.5, rsi_flag=1, macd_flag=2, **overrides) -> FeatureSnapshot:
    values = {
        SCORE_EMA: score, RSI_FLAG: rsi_flag, MACD_FLAG: macd_flag,
        "ema_50": 95.0, "ema_200": 90.0, "adx_14": 25.0, "rvol_20": 1.2, "atr_14": 2.0, "atr_10": 2.0,
    }
    values.update(overrides)
    return snap(day=day, close=close, **values)


def ithor_signal(strategy, latest):
    return strategy.signal("T", AssetClass.STOCK, History([ithor_snap(day=0), latest]))


def test_indicathor_entry_with_an_atr_stop():
    signal = ithor_signal(IndicathorStrategy(), ithor_snap())
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss == pytest.approx(96.0)  # 100 - 2 x ATR(14)=2
    assert signal.take_profit is None


@pytest.mark.parametrize(
    ("strategy", "overrides", "blocked_by"),
    [
        (IndicathorStrategy(), {"ema_200": 101.0}, "trend"),
        (IndicathorStrategy(trend_filter="cross"), {"ema_50": 89.0}, "trend"),
        (IndicathorStrategy(), {"adx_14": 15.0}, "adx"),
        (IndicathorStrategy(rvol_min=1.5), {}, "volume"),
        (IndicathorStrategy(use_rs=True), {RELATIVE_STRENGTH: -3.0}, "relative_strength"),
    ],
)
def test_indicathor_filters_block_entries(strategy, overrides, blocked_by):
    signal = ithor_signal(strategy, ithor_snap(**overrides))
    assert signal.action == SignalAction.HOLD
    assert signal.reason.startswith(f"entry_filtered_{blocked_by}")


def test_indicathor_filters_can_be_switched_off():
    strategy = IndicathorStrategy(trend_filter="none", adx_min=0)
    assert ithor_signal(strategy, ithor_snap(ema_200=150.0, adx_14=5.0)).action == SignalAction.BUY


def test_relative_strength_is_skipped_without_a_benchmark():
    # Crypto (or SPY itself) has no benchmark: the column is absent and does not block.
    assert ithor_signal(IndicathorStrategy(use_rs=True), ithor_snap()).action == SignalAction.BUY


@pytest.mark.parametrize(
    ("score", "rsi_flag", "macd_flag", "action"),
    [
        (0.9, 1, 2, SignalAction.HOLD),  # below the entry score
        (1.5, -1, 2, SignalAction.HOLD),  # RSI flag not positive
        (-0.1, 1, 1, SignalAction.SELL),  # score below the exit level
        (0.5, -1, -1, SignalAction.SELL),  # both flags non-positive
    ],
)
def test_indicathor_score_and_flag_rules(score, rsi_flag, macd_flag, action):
    latest = ithor_snap(score=score, rsi_flag=rsi_flag, macd_flag=macd_flag)
    assert ithor_signal(IndicathorStrategy(), latest).action == action


def test_indicathor_trailing_stop_uses_bars_since_entry_only():
    strategy = IndicathorStrategy(trail_lookback=2)
    entry_bar = snap(day=0, high=150.0, atr_10=2.0)  # its high predates the entry
    held = [snap(day=1, high=110.0, atr_10=2.0), snap(day=2, high=112.0, atr_10=2.0),
            snap(day=3, high=111.0, atr_10=2.0)]
    position = OpenPosition("T", AssetClass.STOCK, "1Day", 100.0, entry_bar.timestamp, stop_loss=96.0)

    plan = strategy.manage(position, History([entry_bar, *held]))

    assert plan.stop_loss == pytest.approx(112.0 - 6.0)  # last 2 bars: max(high) - 3 x ATR(10)


def test_indicathor_benchmark_and_relative_strength_column():
    strategy = IndicathorStrategy(use_rs=True, rs_length=5)
    assert strategy.benchmark_for("AAPL", AssetClass.STOCK) == "SPY"
    assert strategy.benchmark_for("SPY", AssetClass.STOCK) is None
    assert strategy.benchmark_for("BTC/USD", AssetClass.CRYPTO) is None
    assert IndicathorStrategy().benchmark_for("AAPL", AssetClass.STOCK) is None

    frame = pd.DataFrame({
        "rsi_14": 55.0, "macd_12_26_9": 1.0, "macd_12_26_9_signal": 0.5,
        "roc_5": 10.0,
    }, index=range(10))
    benchmark = pd.Series(np.linspace(100, 110, 10))
    derived = strategy.derive(frame, MarketContext(benchmark_close=benchmark))
    expected = 10.0 - (benchmark / benchmark.shift(5) - 1) * 100
    pd.testing.assert_series_equal(derived[RELATIVE_STRENGTH], expected, check_names=False)


@pytest.mark.parametrize(
    "params",
    [
        {"macd_flag_mode": "sometimes"},
        {"trend_filter": "maybe"},
        {"stop_trigger": "open"},
        {"ema_fast": 200, "ema_slow": 50},
        {"entry_score": 0.0, "exit_score": 0.0},
        {"trail_atr_mult": 0},
    ],
)
def test_indicathor_rejects_bad_parameters(params):
    with pytest.raises(ValueError):
        get_strategy("indicathor", params)


def test_indicathor_min_bars_follows_its_filters():
    assert IndicathorStrategy().min_bars == 210
    assert IndicathorStrategy(trend_filter="none").min_bars == 45


# --- rsi2_pullback ------------------------------------------------------------------------------


def rsi2_snap(close=100.0, rsi=5.0, trend=90.0, exit_sma=105.0, day=1):
    return snap(day=day, close=close, rsi_2=rsi, sma_200=trend, sma_5=exit_sma, atr_14=2.0)


@pytest.mark.parametrize(
    ("latest", "action"),
    [
        (rsi2_snap(), SignalAction.BUY),
        (rsi2_snap(rsi=20.0), SignalAction.HOLD),
        (rsi2_snap(trend=101.0), SignalAction.HOLD),  # below the long-term trend
        (rsi2_snap(exit_sma=99.0), SignalAction.SELL),  # close above the exit SMA
    ],
)
def test_rsi2_rules(latest, action):
    signal = Rsi2PullbackStrategy().signal("T", AssetClass.STOCK, History([rsi2_snap(day=0), latest]))
    assert signal.action == action
    if action == SignalAction.BUY:
        assert signal.stop_loss == pytest.approx(94.0)  # 3 x ATR(14)


def test_rsi2_without_stop_and_with_a_time_exit():
    strategy = Rsi2PullbackStrategy(stop_atr_mult=0, max_hold_bars=2)
    assert strategy.risk_levels(100.0, rsi2_snap()) == (None, None)
    position = OpenPosition("T", AssetClass.STOCK, "1Day", 100.0, TS)
    assert strategy.manage(position, History([snap(day=0), snap(day=1)])) is None
    plan = strategy.manage(position, History([snap(day=0), snap(day=1), snap(day=2)]))
    assert plan.exit_reason.startswith("max_holding_period")


# --- donchian_breakout ---------------------------------------------------------------------------


def dc(day, close, upper, lower):
    return snap(day=day, close=close, high=close + 0.5, dc_55_20_upper=upper, dc_55_20_lower=lower, atr_20=2.0)


@pytest.mark.parametrize(
    ("previous", "latest", "action"),
    [
        (dc(0, 100, upper=104, lower=90), dc(1, 105, upper=105.5, lower=90), SignalAction.BUY),
        (dc(0, 100, upper=106, lower=90), dc(1, 105, upper=106, lower=90), SignalAction.HOLD),
        (dc(0, 95, upper=106, lower=92), dc(1, 91, upper=106, lower=90.5), SignalAction.SELL),
    ],
)
def test_donchian_compares_with_the_previous_channel(previous, latest, action):
    signal = DonchianBreakoutStrategy().signal("T", AssetClass.STOCK, History([previous, latest]))
    assert signal.action == action
    if action == SignalAction.BUY:
        assert signal.stop_loss == pytest.approx(101.0)  # 105 - 2 x ATR(20)


def test_donchian_rejects_an_exit_channel_longer_than_the_entry():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy(entry_length=20, exit_length=55)


# --- end to end ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("indicathor", {}),
        ("indicathor", {"macd_flag_mode": "cross", "trend_filter": "cross", "stop_trigger": "close"}),
        ("rsi2_pullback", {"max_hold_bars": 5}),
        ("donchian_breakout", {}),
    ],
)
def test_strategies_trade_in_backtests_and_run_live(name, params, tmp_path):
    pytest.importorskip("backtesting")
    bars = random_walk_bars(600)
    config = BacktestConfig("TEST", AssetClass.STOCK, strategy=name, strategy_params=params,
                            risk_per_trade=10.0, trade_on_close=True)

    result = run_strategy_backtest(bars, config, chart_features=False)

    assert len(result.trades) >= 2
    assert result.strategy_params == get_strategy(name, params).params()

    storage = Storage(tmp_path / "db.sqlite3")
    storage.initialize()
    live = run_once(
        broker=FakeBroker(bars), notifier=RecordingNotifier(), symbol="TEST", asset_class=AssetClass.STOCK,
        timeframe="1Day", limit=600, orders_enabled=False, notional=100.0, storage=storage,
        now=after_close(bars), strategy=get_strategy(name, params),
    )
    assert live.status == "processed"
    assert "insufficient_indicator_data" not in live.decision.reason
