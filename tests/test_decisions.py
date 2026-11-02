from datetime import UTC, datetime, timedelta

import pytest

from trading_pipeline.decisions import decide
from trading_pipeline.domain import (
    AssetClass,
    DecisionAction,
    ExitReason,
    OpenPosition,
    Signal,
    SignalAction,
)
from trading_pipeline.features import FeatureSnapshot
from trading_pipeline.strategies import History, SmaRsiStrategy

TS = datetime(2026, 3, 2, tzinfo=UTC)


def snap(close=100.0, low=None, high=None, **values) -> FeatureSnapshot:
    return FeatureSnapshot(
        timestamp=TS,
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=1,
        values=values,
        patterns=[],
    )


def signal(action: SignalAction, close=100.0) -> Signal:
    return Signal("T", AssetClass.STOCK, action, 0.6, f"{action.value}_reason", close, TS, 96.0, 108.0)


def position(stop=96.0, target=108.0) -> OpenPosition:
    entered = TS - timedelta(days=3)
    return OpenPosition("T", AssetClass.STOCK, "1Day", 100.0, entered, stop_loss=stop, take_profit=target, id=1)


def test_buy_when_flat_enters():
    decision = decide(signal(SignalAction.BUY), None, snap())
    assert decision.action == DecisionAction.ENTER_LONG
    assert (decision.stop_loss, decision.take_profit) == (96.0, 108.0)


def test_sell_when_flat_is_ignored():
    decision = decide(signal(SignalAction.SELL), None, snap())
    assert decision.action == DecisionAction.NONE
    assert decision.reason == "sell_signal_ignored_no_open_position"


def test_buy_when_long_is_ignored():
    decision = decide(signal(SignalAction.BUY), position(), snap())
    assert decision.action == DecisionAction.NONE


def test_sell_when_long_exits():
    decision = decide(signal(SignalAction.SELL), position(), snap())
    assert decision.action == DecisionAction.EXIT_LONG
    assert decision.exit_reason == ExitReason.SIGNAL


def test_stop_loss_touch_exits():
    decision = decide(signal(SignalAction.HOLD), position(), snap(close=97, low=95.9))
    assert decision.exit_reason == ExitReason.STOP_LOSS


def test_take_profit_touch_exits():
    decision = decide(signal(SignalAction.HOLD), position(), snap(close=107, high=108.0))
    assert decision.exit_reason == ExitReason.TAKE_PROFIT


def test_bar_spanning_stop_and_target_assumes_stop():
    decision = decide(signal(SignalAction.HOLD), position(), snap(close=100, low=90, high=120))
    assert decision.exit_reason == ExitReason.STOP_LOSS


def test_hold_while_long_keeps_position():
    decision = decide(signal(SignalAction.HOLD), position(), snap())
    assert decision.action == DecisionAction.NONE
    assert decision.reason.startswith("holding_position")


@pytest.mark.parametrize(
    ("prev", "latest", "rsi", "expected"),
    [
        ((99, 100), (101, 100), 55, SignalAction.BUY),  # cross up
        ((99, 100), (101, 100), 72, SignalAction.HOLD),  # cross up but RSI too high
        ((101, 100), (99, 100), 50, SignalAction.SELL),  # cross down
        ((101, 100), (102, 100), 80, SignalAction.SELL),  # overbought
        ((101, 100), (102, 100), 60, SignalAction.HOLD),
    ],
)
def test_strategy_rules(prev, latest, rsi, expected):
    previous = snap(sma_20=prev[0], sma_50=prev[1], rsi_14=50)
    current = snap(sma_20=latest[0], sma_50=latest[1], rsi_14=rsi)
    result = SmaRsiStrategy().signal("T", AssetClass.STOCK, History([previous, current]))
    assert result.action == expected


def test_strategy_holds_without_indicator_data():
    result = SmaRsiStrategy().signal("T", AssetClass.STOCK, History([snap(), snap()]))
    assert result.reason == "insufficient_indicator_data"


def test_entry_bar_range_does_not_trigger_stop_or_target():
    entered_now = OpenPosition("T", AssetClass.STOCK, "1Day", 100.0, TS, stop_loss=96.0, take_profit=108.0, id=1)
    decision = decide(signal(SignalAction.HOLD), entered_now, snap(close=100, low=90, high=120))
    assert decision.action == DecisionAction.NONE
