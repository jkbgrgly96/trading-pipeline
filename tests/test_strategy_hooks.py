from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pandas as pd
import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, crossover_bars
from trading_pipeline import strategies
from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.decisions import decide, evaluate
from trading_pipeline.domain import (
    AssetClass,
    Bar,
    DecisionAction,
    ExitReason,
    OpenPosition,
    PositionPlan,
    Signal,
    SignalAction,
    StopTrigger,
)
from trading_pipeline.features import FeatureSnapshot, IndicatorDefinition
from trading_pipeline.pipeline import run_once
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import (
    STRATEGIES,
    History,
    Strategy,
    get_strategy,
    hold_signal,
    strategy_frame,
)
from trading_pipeline.watch import Watcher, WatchItem

TS = datetime(2026, 3, 2, tzinfo=UTC)


def snap(close=100.0, low=None, high=None, day=0) -> FeatureSnapshot:
    return FeatureSnapshot(
        TS + timedelta(days=day), close, high if high is not None else close + 1,
        low if low is not None else close - 1, close, 1, {}, [],
    )


def hold() -> Signal:
    return Signal("T", AssetClass.STOCK, SignalAction.HOLD, 0.0, "hold", 100.0, TS)


def long(stop=96.0, target=None) -> OpenPosition:
    return OpenPosition("T", AssetClass.STOCK, "1Day", 100.0, TS - timedelta(days=3),
                        stop_loss=stop, take_profit=target, id=1)


# --- decide: trailing stops, stop trigger, strategy exits --------------------------


def test_a_proposed_stop_is_raised_but_never_lowered():
    raised = decide(hold(), long(stop=96), snap(), plan=PositionPlan(stop_loss=98))
    lowered = decide(hold(), long(stop=96), snap(), plan=PositionPlan(stop_loss=90))

    assert raised.action == lowered.action == DecisionAction.NONE
    assert raised.stop_loss == 98
    assert lowered.stop_loss == 96


def test_this_bar_is_checked_against_the_old_stop():
    # The bar's own high lifts the stop to 99, above its low of 97: that must
    # not count as a hit, since the low may have come before the high.
    decision = decide(hold(), long(stop=96), snap(close=100, low=97), plan=PositionPlan(stop_loss=99))
    assert decision.action == DecisionAction.NONE
    assert decision.stop_loss == 99


@pytest.mark.parametrize(
    ("bar", "trigger", "exits"),
    [
        (snap(close=97, low=95), StopTrigger.INTRABAR, True),
        (snap(close=97, low=95), StopTrigger.CLOSE, False),
        (snap(close=95.5, low=95), StopTrigger.CLOSE, True),
    ],
)
def test_stop_trigger_low_or_close(bar, trigger, exits):
    decision = decide(hold(), long(stop=96), bar, stop_trigger=trigger)
    assert (decision.exit_reason == ExitReason.STOP_LOSS) is exits


def test_close_trigger_also_applies_to_the_target():
    bar = snap(close=107, high=109)
    assert decide(hold(), long(target=108), bar).exit_reason == ExitReason.TAKE_PROFIT
    assert decide(hold(), long(target=108), bar, stop_trigger=StopTrigger.CLOSE).action == DecisionAction.NONE


def test_strategy_exit_closes_the_position():
    decision = decide(hold(), long(), snap(), plan=PositionPlan(exit_reason="max_holding_period"))
    assert decision.exit_reason == ExitReason.STRATEGY
    assert "max_holding_period" in decision.reason


# --- History.since -----------------------------------------------------------------------


def test_history_since_returns_bars_after_the_timestamp():
    snapshots = [snap(day=day) for day in range(6)]
    history = History(snapshots, end=5)  # day 5 is in the future

    assert history.since(TS + timedelta(days=1)) == snapshots[2:5]
    assert history.since(TS + timedelta(days=1), limit=2) == snapshots[3:5]
    assert history.since((TS + timedelta(days=4)).replace(tzinfo=None)) == []


# --- a strategy using every hook ---------------------------------------------------------


@dataclass(frozen=True)
class TrailingStrategy(Strategy):
    """Buys on an up close; trails a stop under the highest close since entry;
    exits after ``max_hold`` bars. Reads a derived column and a benchmark."""

    name: ClassVar[str] = "trailing_test"
    trail_pct: float = 0.03
    max_hold: int = 30
    stop_trigger: str = "low"

    @property
    def features(self):
        return [IndicatorDefinition("sma_5", "sma", {"length": 5})]

    @property
    def min_bars(self) -> int:
        return 10

    def benchmark_for(self, symbol, asset_class):
        return None if symbol == "BENCH" else "BENCH"

    def derive(self, frame, context):
        derived = {"up_close": (frame["Close"] > frame["Close"].shift()).astype(float)}
        if context.benchmark_close is not None:
            derived["vs_bench"] = frame["Close"] / context.benchmark_close
        return derived

    def risk_levels(self, entry_price, latest):
        return entry_price * (1 - 2 * self.trail_pct), None

    def signal(self, symbol, asset_class, history):
        latest = history.latest
        if latest.get("up_close") == 1.0:
            stop, _ = self.risk_levels(latest.close, latest)
            return Signal(symbol, asset_class, SignalAction.BUY, 0.5, "up_close", latest.close,
                          latest.timestamp, stop)
        return hold_signal(symbol, asset_class, latest, "flat")

    def manage(self, position, history):
        held = history.since(position.entry_time)
        if len(held) >= self.max_hold:
            return PositionPlan(exit_reason="max_holding_period")
        best = max(snapshot.close for snapshot in held) if held else position.entry_price
        return PositionPlan(stop_loss=best * (1 - self.trail_pct))


class SymbolBroker(FakeBroker):
    """Different bars per symbol; counts bar requests."""

    def __init__(self, bars_by_symbol):
        super().__init__(next(iter(bars_by_symbol.values())))
        self.bars_by_symbol = bars_by_symbol
        self.requests: list[str] = []

    def get_bars(self, symbol, asset_class, timeframe, limit):
        self.requests.append(symbol)
        return list(self.bars_by_symbol[symbol][-limit:])


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.initialize()
    return store


@pytest.fixture
def trailing(monkeypatch):
    monkeypatch.setitem(STRATEGIES, TrailingStrategy.name, TrailingStrategy)
    return TrailingStrategy()


def benchmark_of(bars: list[Bar]) -> list[Bar]:
    # The benchmark is missing one bar: it is forward-filled, not dropped.
    return [Bar(b.timestamp, 50.0, 51.0, 49.0, 50.0 + i * 0.01, 1) for i, b in enumerate(bars) if i != 40]


def test_strategy_frame_adds_derived_columns_and_aligns_the_benchmark(trailing):
    bars = crossover_bars(60)
    frame = strategy_frame(trailing, bars, benchmark_of(bars), chart_features=False)

    assert {"sma_5", "up_close", "vs_bench"} <= set(frame.columns)
    assert "rsi_14" not in frame.columns  # chart-only defaults skipped
    assert frame["vs_bench"].iloc[40] == pytest.approx(bars[40].close / (50.0 + 39 * 0.01))


def test_derived_columns_may_not_clash_with_features(trailing, monkeypatch):
    monkeypatch.setattr(TrailingStrategy, "derive", lambda self, frame, context: {"sma_5": frame["Close"]})
    with pytest.raises(ValueError, match="clashes"):
        strategy_frame(trailing, crossover_bars(20), chart_features=False)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_registered_strategy_is_causal(name):
    """Indicators and derived columns at a bar never change when later bars arrive."""
    strategy = get_strategy(name)
    bars = crossover_bars(149)
    bench = strategy.benchmark_for("TEST", AssetClass.STOCK)
    bench_bars = benchmark_of(bars) if bench else None
    full = strategy_frame(strategy, bars, bench_bars, chart_features=False)
    cut = len(bars) - 25
    prefix = strategy_frame(strategy, bars[:cut], bench_bars[:cut] if bench_bars else None, chart_features=False)
    pd.testing.assert_frame_equal(prefix, full.iloc[:cut], check_freq=False)


def test_live_and_backtest_take_the_same_trades_with_a_trailing_stop(storage, trailing):
    """The live pipeline, fed one bar at a time, opens and closes positions on the
    same bars as the backtest, including trailing-stop exits; the stored stop
    of each position only ever moves up."""
    bars = crossover_bars(149)
    bench = benchmark_of(bars)
    broker = SymbolBroker({"TEST": [], "BENCH": bench})
    stops: dict[int, list[float]] = {}
    for end in range(trailing.min_bars, len(bars) + 1):
        broker.bars_by_symbol["TEST"] = bars[:end]
        run_once(
            broker=broker, notifier=RecordingNotifier(), symbol="TEST", asset_class=AssetClass.STOCK,
            timeframe="1Day", limit=500, orders_enabled=False, notional=100.0, storage=storage,
            now=after_close(bars[:end]), strategy=trailing,
        )
        position = storage.get_open_position("TEST", AssetClass.STOCK, "1Day")
        if position is not None:
            stops.setdefault(position.id, []).append(position.stop_loss)

    result = run_strategy_backtest(
        bars, BacktestConfig("TEST", AssetClass.STOCK, strategy=trailing.name, trade_on_close=True),
        benchmark_bars=bench, chart_features=False,
    )

    with storage.connect() as connection:
        live = connection.execute(
            "SELECT entry_time, exit_time, exit_reason FROM positions WHERE status = 'closed' ORDER BY id"
        ).fetchall()
    backtest = list(zip(result.trades["EntryTime"], result.trades["ExitTime"], strict=True))
    assert len(live) >= 3
    assert {row[2] for row in live} >= {"stop_loss_hit"}
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b, _ in live] == [
        (pd.Timestamp(a), pd.Timestamp(b)) for a, b in backtest[: len(live)]
    ]
    assert any(len(set(levels)) > 1 for levels in stops.values())
    assert all(levels == sorted(levels) for levels in stops.values())


def test_watcher_fetches_a_shared_benchmark_once_per_cycle(storage, trailing):
    bars = crossover_bars(60)
    broker = SymbolBroker({"AAA": bars, "BBB": bars, "BENCH": benchmark_of(bars)})
    watcher = Watcher(
        broker, storage, RecordingNotifier(),
        [WatchItem("AAA", AssetClass.STOCK, "1Day", strategy="trailing_test"),
         WatchItem("BBB", AssetClass.STOCK, "1Day", strategy="trailing_test")],
        orders_enabled=False, clock=lambda: after_close(bars),
    )

    watcher.run_cycle()

    assert broker.requests.count("BENCH") == 1
    assert sorted(broker.requests) == ["AAA", "BBB", "BENCH"]


def test_evaluate_uses_the_strategy_trigger(trailing):
    position = long(stop=96)
    history = History([snap(day=0), snap(close=97, low=95, day=1)])
    close_strategy = TrailingStrategy(stop_trigger="close")
    assert evaluate(trailing, "T", AssetClass.STOCK, history, position).exit_reason == ExitReason.STOP_LOSS
    assert evaluate(close_strategy, "T", AssetClass.STOCK, history, position).action == DecisionAction.NONE


def test_strategies_module_exports_the_registry():
    assert strategies.get_strategy is get_strategy
