from __future__ import annotations

import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, crossover_bars
from trading_pipeline.backtest.runner import BacktestConfig, run_strategy_backtest
from trading_pipeline.domain import AssetClass, DecisionAction
from trading_pipeline.pipeline import run_once
from trading_pipeline.sizing import Sizing, SizingMode
from trading_pipeline.storage import Storage
from trading_pipeline.watch import Watcher, WatchItem

RISK = Sizing(SizingMode.RISK, notional=100.0, risk_per_trade=10.0, max_notional=1_000.0)


@pytest.mark.parametrize(
    ("sizing", "price", "stop", "expected"),
    [
        (RISK, 100.0, 96.0, 250.0),  # $10 / 4% = $250
        (RISK, 100.0, 99.5, 1_000.0),  # $2,000 would be needed: capped
        (RISK, 100.0, None, 100.0),  # no stop: the fixed notional
        (RISK, 100.0, 101.0, 100.0),  # a stop above the price is not usable
        (Sizing(SizingMode.NOTIONAL, notional=75.0), 100.0, 96.0, 75.0),
    ],
)
def test_entry_notional(sizing, price, stop, expected):
    assert sizing.entry_notional(price, stop) == pytest.approx(expected)


def test_describe_shows_the_risk_and_the_cap():
    assert RISK.describe(100.0, 96.0) == "$250.00, risking $10.00 to the stop"
    assert RISK.describe(100.0, 99.5) == "$1,000.00 (capped), risking $5.00 to the stop"
    assert Sizing(notional=100.0).describe(100.0, 96.0) == "$100.00"


@pytest.mark.parametrize("field", ["notional", "risk_per_trade", "max_notional"])
def test_amounts_must_be_positive(field):
    with pytest.raises(ValueError):
        Sizing(SizingMode.RISK, **{field: 0})


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.initialize()
    return store


def test_paper_order_alert_and_position_use_the_risk_size(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()

    result = run_once(
        broker=broker, notifier=notifier, symbol="TEST", asset_class=AssetClass.STOCK,
        timeframe="1Day", limit=500, orders_enabled=True, notional=100.0, storage=storage,
        now=after_close(bars), sizing=RISK,
    )

    assert result.decision.action == DecisionAction.ENTER_LONG  # sma_rsi: stop 4% below
    ((symbol, notional),) = broker.buys
    assert (symbol, notional) == ("TEST", pytest.approx(250.0))
    assert "Size: $250.00, risking $10.00 to the stop" in notifier.messages[-1]
    with storage.connect() as connection:
        assert connection.execute("SELECT notional FROM positions").fetchone()[0] == pytest.approx(250.0)


def test_alerts_only_positions_record_the_suggested_size(storage):
    bars = crossover_bars()
    notifier = RecordingNotifier()
    watcher = Watcher(
        FakeBroker(bars), storage, notifier,
        [WatchItem("TEST", AssetClass.STOCK, "1Day", sizing_mode="risk", risk_per_trade=20.0)],
        orders_enabled=False, clock=lambda: after_close(bars),
    )

    watcher.run_cycle()

    assert "Size: $500.00, risking $20.00 to the stop" in notifier.messages[-1]
    with storage.connect() as connection:
        assert connection.execute("SELECT notional FROM positions").fetchone()[0] == pytest.approx(500.0)


def test_backtest_risk_sizing_matches_paper_orders():
    pytest.importorskip("backtesting")
    config = BacktestConfig(
        "TEST", AssetClass.STOCK, risk_per_trade=10.0, max_notional=1_000.0, trade_on_close=True
    )

    result = run_strategy_backtest(crossover_bars(149), config)

    assert config.sizing == "risk"
    first = result.trades.iloc[0]
    assert first["Size"] * first["EntryPrice"] == pytest.approx(250.0, abs=0.01)


def test_backtest_rejects_a_cap_above_the_cash():
    with pytest.raises(ValueError, match="max_notional"):
        BacktestConfig("TEST", AssetClass.STOCK, cash=500, risk_per_trade=10.0).validate()


def test_watchlist_sizing_settings(tmp_path):
    from trading_pipeline.watch import load_watchlist

    path = tmp_path / "watchlist.toml"
    path.write_text(
        '[defaults]\nsizing = "risk"\nrisk_per_trade = 15\n\n'
        '[[watch]]\nsymbol = "AAPL"\n\n'
        '[[watch]]\nsymbol = "MSFT"\nsizing = "notional"\nnotional = 50\n',
        encoding="utf-8",
    )
    aapl, msft = load_watchlist(path, 250, 100)
    assert aapl.sizing == Sizing(SizingMode.RISK, 100.0, 15.0, 1_000.0)
    assert msft.sizing.mode == SizingMode.NOTIONAL and msft.sizing.notional == 50.0


def test_watchlist_rejects_an_unknown_sizing(tmp_path):
    from trading_pipeline.watch import load_watchlist

    path = tmp_path / "watchlist.toml"
    path.write_text('[[watch]]\nsymbol = "AAPL"\nsizing = "all-in"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_watchlist(path, 250, 100)
