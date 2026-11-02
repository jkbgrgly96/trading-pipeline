from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, crash_bar, crossover_bars
from trading_pipeline.brokers.base import BrokerPosition
from trading_pipeline.config import load_settings
from trading_pipeline.dashboard import queries
from trading_pipeline.domain import AssetClass, DecisionAction
from trading_pipeline.pipeline import run_once, sync_order_fills
from trading_pipeline.storage import Storage
from trading_pipeline.watch import Watcher, WatchItem

FILL_TIME = datetime(2026, 4, 9, 13, 30, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.initialize()
    return store


def run(broker, notifier, storage, now, **kwargs):
    return run_once(
        broker=broker, notifier=notifier, symbol="TEST", asset_class=AssetClass.STOCK,
        timeframe="1Day", limit=500, notional=100.0, storage=storage, now=now,
        **{"orders_enabled": False, **kwargs},
    )


def open_other_position(storage):
    storage.open_position(
        symbol="OTHER", asset_class=AssetClass.STOCK, timeframe="1Day", entry_price=10.0,
        entry_time=datetime(2026, 1, 5, tzinfo=UTC), stop_loss=9.0, take_profit=11.0,
        source="virtual",
    )


# --- max open positions -------------------------------------------------------


def test_buy_is_skipped_while_the_open_position_cap_is_reached(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    open_other_position(storage)

    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True, max_open_positions=1)

    assert result.decision.action == DecisionAction.NONE
    assert result.decision.reason.startswith("buy_signal_skipped_max_open_positions")
    assert notifier.messages == []
    assert broker.buys == []
    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is None


def test_buy_is_taken_below_the_cap(storage):
    bars = crossover_bars()
    open_other_position(storage)

    result = run(FakeBroker(bars), RecordingNotifier(), storage, after_close(bars), max_open_positions=2)

    assert result.decision.action == DecisionAction.ENTER_LONG


def test_exits_are_never_blocked_by_the_cap(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    run(broker, notifier, storage, after_close(bars), max_open_positions=1)

    bars.append(crash_bar(bars[-1]))
    result = run(broker, notifier, storage, after_close(bars), max_open_positions=1)

    assert result.decision.action == DecisionAction.EXIT_LONG


def test_watcher_passes_the_cap_on(storage):
    bars = crossover_bars()
    open_other_position(storage)
    notifier = RecordingNotifier()
    watcher = Watcher(
        FakeBroker(bars), storage, notifier, [WatchItem("TEST", AssetClass.STOCK, "1Day")],
        orders_enabled=False, clock=lambda: after_close(bars), max_open_positions=1,
    )

    assert watcher.run_cycle() == {"TEST 1Day (stock)": "none"}


def test_max_open_positions_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
    assert load_settings().max_open_positions == 3
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "0")
    assert load_settings().max_open_positions is None
    monkeypatch.delenv("MAX_OPEN_POSITIONS")
    assert load_settings().max_open_positions is None


# --- fill prices ----------------------------------------------------------------


def enter_on_paper(storage, broker, notifier, bars):
    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    assert result.decision.action == DecisionAction.ENTER_LONG
    return result.order_id


def test_entry_and_exit_fills_are_recorded_and_used_for_pnl(storage, tmp_path):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    entry_order = enter_on_paper(storage, broker, notifier, bars)

    assert sync_order_fills(storage, broker) == 0  # still "accepted": no fill yet

    alert_price = bars[-1].close
    broker.order_statuses[entry_order] = "filled"
    broker.fills[entry_order] = (0.75, alert_price * 1.01, FILL_TIME)
    broker.positions["TEST"] = BrokerPosition("TEST", 0.75, alert_price * 1.01)
    assert sync_order_fills(storage, broker) == 1
    position = storage.get_open_position("TEST", AssetClass.STOCK, "1Day")
    assert position.entry_fill_price == pytest.approx(alert_price * 1.01)
    assert position.entry_price == pytest.approx(alert_price)  # the alert price is kept

    lookups = len(broker.order_lookups)
    assert sync_order_fills(storage, broker) == 0
    assert len(broker.order_lookups) == lookups  # recorded fills are not polled again

    bars.append(crash_bar(bars[-1]))
    exit_result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    assert exit_result.decision.action == DecisionAction.EXIT_LONG
    assert "Entry (paper fill):" in notifier.messages[-1]

    exit_alert_price = bars[-1].close
    broker.fills[exit_result.order_id] = (0.75, exit_alert_price * 0.99, FILL_TIME + timedelta(days=1))
    assert sync_order_fills(storage, broker) == 1

    closed = queries.load_positions(tmp_path / "db.sqlite3", status="closed").iloc[0]
    assert closed["quantity"] == 0.75
    assert closed["pnl_pct"] == pytest.approx((0.99 * exit_alert_price / (1.01 * alert_price) - 1) * 100)
    assert closed["entry_fill_vs_alert_pct"] == pytest.approx(1.0)
    assert closed["exit_fill_vs_alert_pct"] == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("status", "fill", "recorded"),
    [
        ("partially_filled", (0.3, 100.0), False),  # may still fill more
        ("canceled", (0.3, 100.0), True),  # partial fill, then dead: final
        ("canceled", None, False),  # nothing filled
        ("new", None, False),
    ],
)
def test_only_final_fills_are_recorded(storage, status, fill, recorded):
    bars = crossover_bars()
    broker = FakeBroker(bars)
    order_id = enter_on_paper(storage, broker, RecordingNotifier(), bars)
    broker.order_statuses[order_id] = status
    if fill:
        broker.fills[order_id] = (*fill, FILL_TIME)

    assert sync_order_fills(storage, broker) == int(recorded)


def test_old_orders_are_not_polled(storage):
    bars = crossover_bars()
    broker = FakeBroker(bars)
    enter_on_paper(storage, broker, RecordingNotifier(), bars)

    sync_order_fills(storage, broker, now=datetime.now(UTC) + timedelta(days=8))

    assert broker.order_lookups == []


def test_a_failing_lookup_does_not_block_other_fills(storage):
    class FlakyBroker(FakeBroker):
        def get_order(self, order_id):
            if order_id == "order-1":
                raise RuntimeError("timeout")
            return super().get_order(order_id)

    bars = crossover_bars()
    broker = FlakyBroker(bars)
    enter_on_paper(storage, broker, RecordingNotifier(), bars)  # order-1
    storage.open_position(
        symbol="OTHER", asset_class=AssetClass.STOCK, timeframe="1Day", entry_price=10.0,
        entry_time=FILL_TIME, stop_loss=None, take_profit=None, source="paper",
        entry_order_id="order-9",
    )
    broker.fills["order-9"] = (2.0, 10.1, FILL_TIME)

    assert sync_order_fills(storage, broker) == 1


def test_watcher_syncs_fills_when_paper_orders_are_on(storage):
    bars = crossover_bars()
    broker = FakeBroker(bars)
    order_id = enter_on_paper(storage, broker, RecordingNotifier(), bars)
    broker.order_statuses[order_id] = "filled"
    broker.fills[order_id] = (0.5, 200.0, FILL_TIME)
    broker.positions["TEST"] = BrokerPosition("TEST", 0.5, 200.0)
    watcher = Watcher(
        broker, storage, RecordingNotifier(), [WatchItem("TEST", AssetClass.STOCK, "1Day")],
        orders_enabled=True, clock=lambda: after_close(bars),
    )

    watcher.run_cycle()

    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day").entry_fill_price == 200.0


def test_positions_load_from_a_database_without_fill_columns(storage, tmp_path):
    open_other_position(storage)
    path = tmp_path / "db.sqlite3"
    with sqlite3.connect(path) as connection:
        for column in ("entry_fill_price", "entry_filled_at", "exit_fill_price", "exit_filled_at"):
            connection.execute(f"ALTER TABLE positions DROP COLUMN {column}")

    positions = queries.load_positions(path)

    assert positions.iloc[0]["symbol"] == "OTHER"
    assert positions["entry_fill_vs_alert_pct"].isna().all()
