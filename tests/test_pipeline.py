from __future__ import annotations

from datetime import timedelta

import pytest

from fakes import BUY_BAR_INDEX, FakeBroker, RecordingNotifier, after_close, crash_bar, crossover_bars
from trading_pipeline.brokers.base import BrokerPosition
from trading_pipeline.domain import AssetClass, Bar, DecisionAction
from trading_pipeline.pipeline import InsufficientBarsError, flush_outbox, run_once
from trading_pipeline.storage import Storage


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.initialize()
    return store


def run(broker, notifier, storage, now, orders_enabled=False, **kwargs):
    return run_once(
        broker=broker,
        notifier=notifier,
        symbol="TEST",
        asset_class=AssetClass.STOCK,
        timeframe="1Day",
        limit=500,
        orders_enabled=orders_enabled,
        notional=100.0,
        storage=storage,
        now=now,
        **kwargs,
    )


def test_buy_signal_when_flat_sends_one_entry_alert_and_opens_virtual_position(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()

    result = run(broker, notifier, storage, after_close(bars))

    assert result.decision.action == DecisionAction.ENTER_LONG
    assert len(notifier.messages) == 1
    assert "BUY TEST" in notifier.messages[0]
    assert "not placed (alerts only)" in notifier.messages[0]
    position = storage.get_open_position("TEST", AssetClass.STOCK, "1Day")
    assert position is not None and position.source == "virtual"
    assert position.stop_loss == pytest.approx(bars[-1].close * 0.96)
    assert broker.buys == []  # alerts only: never an order


def test_rerun_on_same_bar_is_idempotent(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()

    run(broker, notifier, storage, after_close(bars))
    second = run(broker, notifier, storage, after_close(bars) + timedelta(minutes=5))

    assert second.status == "already_processed"
    assert len(notifier.messages) == 1


def test_forming_bar_is_ignored(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    # The BUY bar has opened but not closed yet: evaluate the previous bar.
    now = bars[-1].timestamp + timedelta(hours=6)

    result = run(broker, notifier, storage, now)

    assert result.bar_timestamp == bars[-2].timestamp
    assert result.decision.action == DecisionAction.NONE
    assert notifier.messages == []


def test_hold_is_stored_but_not_notified_unless_requested(storage, tmp_path):
    bars = crossover_bars(BUY_BAR_INDEX)  # stop one bar before the cross
    notifier = RecordingNotifier()

    result = run(FakeBroker(bars), notifier, storage, after_close(bars))
    assert result.decision.action == DecisionAction.NONE
    assert notifier.messages == []

    other = Storage(tmp_path / "hold.sqlite3")
    other.initialize()
    notifier_hold = RecordingNotifier()
    run(FakeBroker(bars), notifier_hold, other, after_close(bars), notify_hold=True)
    assert len(notifier_hold.messages) == 1 and "HOLD" in notifier_hold.messages[0]


def test_stop_loss_on_next_bar_sends_exit_and_closes_position(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    run(broker, notifier, storage, after_close(bars))

    bars.append(crash_bar(bars[-1]))
    result = run(broker, notifier, storage, after_close(bars))

    assert result.decision.action == DecisionAction.EXIT_LONG
    assert result.decision.reason.startswith("stop_loss_hit")
    assert "SELL TEST" in notifier.messages[-1]
    assert "P/L: -" in notifier.messages[-1]
    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is None


def test_sell_signal_without_position_is_not_an_alert(storage):
    bars = crossover_bars(BUY_BAR_INDEX)
    bars.append(crash_bar(bars[-1], drop=0.01))
    # RSI-overbought or cross-down sells only matter when long; here we just
    # assert no exit alert can be produced while flat.
    notifier = RecordingNotifier()
    result = run(FakeBroker(bars), notifier, storage, after_close(bars))
    assert result.decision.action != DecisionAction.EXIT_LONG


def test_failed_notification_is_retried_from_outbox(storage):
    bars = crossover_bars()
    notifier = RecordingNotifier(failures=1)

    result = run(FakeBroker(bars), notifier, storage, after_close(bars))
    assert result.notified is False
    assert storage.pending_notifications()

    assert flush_outbox(storage, notifier) == 1
    assert notifier.messages[0].startswith("(delayed) ")
    assert storage.pending_notifications() == []


def test_paper_orders_buy_then_close(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()

    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    assert broker.buys == [("TEST", 100.0)]
    assert f"submitted ({result.order_id})" in notifier.messages[-1]

    # Order filled at the broker.
    broker.positions["TEST"] = BrokerPosition("TEST", 1.2, bars[-1].close)
    broker.order_statuses[result.order_id] = "filled"
    bars.append(crash_bar(bars[-1]))
    exit_result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert exit_result.decision.action == DecisionAction.EXIT_LONG
    assert broker.closes == ["TEST"]
    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is None


def test_non_paper_broker_never_places_orders(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars, paper=False), RecordingNotifier()

    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert broker.buys == []
    assert "paper account" in result.order_error
    assert "FAILED" in notifier.messages[-1]
    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is None


def test_failed_entry_order_does_not_leave_a_position(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    broker.fail_orders = True

    run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is None
    assert "FAILED" in notifier.messages[-1]


def test_existing_paper_position_is_adopted_and_buy_is_suppressed(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    entry = bars[-1].close
    broker.positions["TEST"] = BrokerPosition("TEST", 2.0, entry)

    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert result.decision.action == DecisionAction.NONE
    assert result.decision.reason == "buy_signal_ignored_already_in_position"
    assert broker.buys == []
    position = storage.get_open_position("TEST", AssetClass.STOCK, "1Day")
    assert position.source == "paper_adopted"
    assert position.stop_loss == pytest.approx(entry * 0.96)


def test_adopted_position_checks_levels_from_the_next_bar(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    broker.positions["TEST"] = BrokerPosition("TEST", 2.0, bars[-1].high / 1.08 - 0.01)

    adopted = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    # The adoption bar's own high predates the adoption: no exit yet.
    assert adopted.decision.action == DecisionAction.NONE
    assert "Tracking paper position TEST" in notifier.messages[0]

    bars.append(Bar(bars[-1].timestamp + timedelta(days=1), 86, 95, 86, 94, 1000))
    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    assert result.decision.reason.startswith("take_profit_hit")
    assert broker.closes == ["TEST"]


def test_pending_exit_order_does_not_cause_a_second_exit(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    first = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    broker.order_statuses[first.order_id] = "filled"
    broker.positions["TEST"] = BrokerPosition("TEST", 1.0, bars[-1].close)

    bars.append(crash_bar(bars[-1]))
    run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    exit_order = broker.closes and f"order-{broker._order_seq}"
    # The close order is queued (after hours): the paper position is still there.
    broker.positions["TEST"] = BrokerPosition("TEST", 1.0, bars[-2].close)
    broker.order_statuses[exit_order] = "accepted"

    bars.append(crash_bar(bars[-1], drop=0.02))
    third = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert third.decision.action == DecisionAction.NONE
    assert broker.closes == ["TEST"]
    assert sum("SELL TEST" in m for m in notifier.messages) == 1


def test_exit_cancels_a_still_queued_entry_order(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    first = run(broker, notifier, storage, after_close(bars), orders_enabled=True)  # accepted

    bars.append(crash_bar(bars[-1]))
    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert result.decision.action == DecisionAction.EXIT_LONG
    assert broker.canceled == [first.order_id]
    assert broker.order_statuses[first.order_id] == "canceled"


def test_enabling_paper_orders_keeps_virtual_positions(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    run(broker, notifier, storage, after_close(bars), orders_enabled=False)  # virtual entry

    bars.append(crash_bar(bars[-1]))
    result = run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert result.decision.reason.startswith("stop_loss_hit")
    assert "SELL TEST" in notifier.messages[-1]


def test_missed_bars_are_replayed_so_the_buy_alert_is_not_lost(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars[:-2]), RecordingNotifier()
    run(broker, notifier, storage, after_close(broker.bars))  # processes bar 95

    broker.bars = bars + [Bar(bars[-1].timestamp + timedelta(days=1), 85, 86, 84.9, 85.5, 1000)]
    result = run(broker, notifier, storage, after_close(broker.bars))  # bars 96, 97 (BUY), 98

    assert result.bars_processed == 3
    assert len(notifier.messages) == 1
    assert notifier.messages[0].startswith("(late: bar was missed")
    assert "BUY TEST" in notifier.messages[0]
    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is not None


def test_first_run_does_not_replay_history(storage):
    bars = crossover_bars() + [crash_bar(crossover_bars()[-1])]
    notifier = RecordingNotifier()
    result = run(FakeBroker(bars), notifier, storage, after_close(bars))
    assert result.bars_processed == 1


def test_undeliverable_alerts_are_dropped_after_max_age_with_a_notice(storage):
    bars = crossover_bars()
    run(FakeBroker(bars), RecordingNotifier(failures=1), storage, after_close(bars))

    notifier = RecordingNotifier()
    sent = flush_outbox(storage, notifier, now=after_close(bars) + timedelta(days=400))

    assert sent == 0
    assert "could not be delivered" in notifier.messages[0]
    assert storage.pending_notifications() == []


def test_local_position_closed_when_paper_account_is_flat(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    first = run(broker, notifier, storage, after_close(bars), orders_enabled=True)
    broker.order_statuses[first.order_id] = "filled"  # filled, then sold by hand

    bars.append(Bar(bars[-1].timestamp + timedelta(days=1), 85, 86, 84.5, 85.5, 1000))
    run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    with storage.connect() as connection:
        row = connection.execute("SELECT exit_reason FROM positions").fetchone()
    assert row["exit_reason"] == "closed_outside_pipeline"


def test_pending_entry_order_keeps_position(storage):
    bars = crossover_bars()
    broker, notifier = FakeBroker(bars), RecordingNotifier()
    run(broker, notifier, storage, after_close(bars), orders_enabled=True)  # status "accepted"

    bars.append(Bar(bars[-1].timestamp + timedelta(days=1), 85, 86, 84.5, 85.5, 1000))
    run(broker, notifier, storage, after_close(bars), orders_enabled=True)

    assert storage.get_open_position("TEST", AssetClass.STOCK, "1Day") is not None


def test_insufficient_bars_raises(storage):
    bars = crossover_bars(40)
    with pytest.raises(InsufficientBarsError):
        run(FakeBroker(bars), RecordingNotifier(), storage, after_close(bars))
