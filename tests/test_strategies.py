from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, crossover_bars
from trading_pipeline import strategies
from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.domain import AssetClass, DecisionAction, Signal, SignalAction
from trading_pipeline.features import (
    DEFAULT_FEATURES,
    FeatureSnapshot,
    IndicatorDefinition,
    merge_definitions,
)
from trading_pipeline.pipeline import run_once
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import (
    History,
    SmaRsiStrategy,
    Strategy,
    available_strategies,
    get_strategy,
    hold_signal,
)

TS = datetime(2026, 3, 2, tzinfo=UTC)


def snap(day: int) -> FeatureSnapshot:
    return FeatureSnapshot(TS + timedelta(days=day), 1, 2, 0, 1, 1, {}, [])


@dataclass(frozen=True)
class BuyOnceStrategy(Strategy):
    """Buys whenever flat, with an ATR stop; records what it was shown."""

    name: ClassVar[str] = "buy_once"
    seen: list[tuple[int, datetime]] = field(default_factory=list, compare=False)

    @property
    def features(self) -> list[IndicatorDefinition]:
        return [IndicatorDefinition("sma_5", "sma", {"length": 5})]

    @property
    def min_bars(self) -> int:
        return 20

    def risk_levels(self, entry_price, latest):
        atr = latest.get("atr_14")
        return entry_price - 2 * atr, None

    def signal(self, symbol, asset_class, history: History) -> Signal:
        latest = history.latest
        self.seen.append((len(history), latest.timestamp))
        assert latest.get("sma_5") is not None
        stop, target = self.risk_levels(latest.close, latest)
        return Signal(
            symbol, asset_class, SignalAction.BUY, 0.5, "always", latest.close,
            latest.timestamp, stop, target,
        )


class NeverStrategy(BuyOnceStrategy):
    name: ClassVar[str] = "never"

    def signal(self, symbol, asset_class, history: History) -> Signal:
        self.seen.append((len(history), history.latest.timestamp))
        return hold_signal(symbol, asset_class, history.latest, "never")


# --- History ------------------------------------------------------------


def test_history_hides_bars_after_the_evaluated_one():
    snapshots = [snap(day) for day in range(5)]
    history = History(snapshots, end=3)

    assert len(history) == 3
    assert history.latest is snapshots[2]
    assert history.previous is snapshots[1]
    assert history[0] is snapshots[0]
    assert history[-3:] == snapshots[:3]
    assert history[1:] == snapshots[1:3]  # open slice stops at the evaluated bar
    assert list(history) == snapshots[:3]
    with pytest.raises(IndexError):
        history[3]
    with pytest.raises(IndexError):
        history[-4]


@pytest.mark.parametrize("end", [0, 6])
def test_history_rejects_end_outside_the_list(end):
    with pytest.raises(ValueError):
        History([snap(day) for day in range(5)], end=end)


# --- registry and features -------------------------------------------------


def test_registry_resolves_default_and_rejects_unknown_names():
    assert "sma_rsi" in available_strategies()
    assert isinstance(get_strategy("sma_rsi"), SmaRsiStrategy)
    with pytest.raises(ValueError, match="Available: .*sma_rsi"):
        get_strategy("nope")


def test_merge_definitions_dedupes_identical_and_rejects_conflicts():
    sma = IndicatorDefinition("sma_20", "sma", {"length": 20})
    assert merge_definitions([sma], [sma]) == [sma]
    with pytest.raises(ValueError, match="sma_20"):
        merge_definitions([sma], [IndicatorDefinition("sma_20", "sma", {"length": 21})])


def test_sma_rsi_defaults_match_the_dashboard_features():
    strategy = SmaRsiStrategy()
    assert strategy.min_bars == 60
    assert strategy.feature_definitions() == DEFAULT_FEATURES


def test_sma_rsi_parameters_change_features_and_rules():
    strategy = SmaRsiStrategy(fast=10, slow=30)
    names = [definition.name for definition in strategy.feature_definitions()]
    assert "sma_10" in names and "sma_30" in names
    assert strategy.min_bars == 40

    def at(fast, slow) -> FeatureSnapshot:
        return FeatureSnapshot(TS, 1, 2, 0, 1, 1, {"sma_10": fast, "sma_30": slow, "rsi_14": 50}, [])

    signal = strategy.signal("T", AssetClass.STOCK, History([at(9, 10), at(11, 10)]))
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss == pytest.approx(0.96)


# --- pipeline and backtest use the selected strategy ------------------------


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.initialize()
    return store


def test_pipeline_runs_the_given_strategy_and_records_its_name(storage):
    bars = crossover_bars(40)
    notifier, strategy = RecordingNotifier(), BuyOnceStrategy()

    result = run_once(
        broker=FakeBroker(bars), notifier=notifier, symbol="TEST", asset_class=AssetClass.STOCK,
        timeframe="1Day", limit=100, orders_enabled=False, notional=100.0, storage=storage,
        now=after_close(bars), strategy=strategy,
    )

    assert result.decision.action == DecisionAction.ENTER_LONG
    assert strategy.seen == [(40, bars[-1].timestamp)]  # 40 bars < sma_rsi's 60
    assert "buy_once" in notifier.messages[0]
    position = storage.get_open_position("TEST", AssetClass.STOCK, "1Day")
    assert position.take_profit is None
    assert position.stop_loss < bars[-1].close
    with storage.connect() as connection:
        assert connection.execute("SELECT strategy FROM signals").fetchone()[0] == "buy_once"
        assert connection.execute("SELECT strategy FROM positions").fetchone()[0] == "buy_once"


def test_backtest_runs_the_configured_strategy_without_look_ahead(storage, monkeypatch):
    pytest.importorskip("backtesting")
    bars = crossover_bars(60)
    never = NeverStrategy()
    monkeypatch.setitem(strategies.STRATEGIES, "never", lambda: never)
    config = BacktestConfig(symbol="TEST", asset_class=AssetClass.STOCK, strategy="never")

    result = run_strategy_backtest(bars, config)
    run_id = storage.save_backtest("TEST", AssetClass.STOCK, "1Day", 60, config, result)

    assert result.stats["# Trades"] == 0
    # Each bar is evaluated once, seeing exactly the bars up to itself.
    lengths = [length for length, _ in never.seen]
    assert lengths == list(range(never.min_bars, never.min_bars + len(lengths)))
    assert len(lengths) >= len(bars) - never.min_bars
    assert all(timestamp == bars[length - 1].timestamp for length, timestamp in never.seen)
    with storage.connect() as connection:
        row = connection.execute("SELECT strategy FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == "never"


# --- parameters ----------------------------------------------------------------


def test_get_strategy_applies_typed_and_string_params():
    from trading_pipeline.strategies import parse_param_assignments

    typed = get_strategy("sma_rsi", {"fast": 10, "stop_loss_pct": 0.05})
    from_cli = get_strategy("sma_rsi", parse_param_assignments(["fast=10", "stop_loss_pct=0.05"]))

    assert typed == from_cli == SmaRsiStrategy(fast=10, stop_loss_pct=0.05)
    assert typed.params()["fast"] == 10 and typed.params()["slow"] == 50


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"nope": 1}, "has no parameter 'nope'"),
        ({"fast": "ten"}, "must be int"),
        ({"fast": 1.5}, "must be int"),
        ({"fast": True}, "must be int"),
        ({"stop_loss_pct": "x"}, "must be float"),
        ({"fast": 60}, "fast < slow"),  # the strategy's own validation
    ],
)
def test_bad_params_are_rejected(params, message):
    with pytest.raises(ValueError, match=message):
        get_strategy("sma_rsi", params)


def test_param_assignments_must_be_key_value():
    from trading_pipeline.strategies import parse_param_assignments

    with pytest.raises(ValueError, match="key=value"):
        parse_param_assignments(["fast"])


def test_pipeline_stores_the_strategy_params(storage):
    import json

    bars = crossover_bars()
    run_once(
        broker=FakeBroker(bars), notifier=RecordingNotifier(), symbol="TEST",
        asset_class=AssetClass.STOCK, timeframe="1Day", limit=500, orders_enabled=False,
        notional=100.0, storage=storage, now=after_close(bars),
        strategy=get_strategy("sma_rsi", {"take_profit_pct": 0.1}),
    )

    with storage.connect() as connection:
        signal_params = connection.execute("SELECT strategy_params FROM signals").fetchone()[0]
        position_params = connection.execute("SELECT strategy_params FROM positions").fetchone()[0]
    assert json.loads(signal_params)["take_profit_pct"] == 0.1
    assert json.loads(position_params) == json.loads(signal_params)
