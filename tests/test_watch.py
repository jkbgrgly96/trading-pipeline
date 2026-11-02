from datetime import UTC, datetime, timedelta

import pytest

from fakes import FakeBroker, RecordingNotifier, after_close, crossover_bars
from trading_pipeline.domain import AssetClass
from trading_pipeline.storage import Storage
from trading_pipeline.watch import (
    Watcher,
    WatchItem,
    default_interval,
    load_watchlist,
    next_run_time,
    validate_watchlist,
)


def test_load_watchlist_applies_defaults(tmp_path):
    path = tmp_path / "watchlist.toml"
    path.write_text(
        """
[defaults]
limit = 300
notional = 50

[[watch]]
symbol = "AAPL"
timeframe = "1Day"

[[watch]]
symbol = "BTC/USD"
asset_class = "crypto"
timeframe = "1Hour"
notional = 25
""",
        encoding="utf-8",
    )
    items = load_watchlist(path, default_limit=250, default_notional=100)
    assert items == [
        WatchItem("AAPL", AssetClass.STOCK, "1Day", 300, 50.0),
        WatchItem("BTC/USD", AssetClass.CRYPTO, "1Hour", 300, 25.0),
    ]
    assert default_interval(items) == timedelta(minutes=15)


def test_same_symbol_twice_rejected_only_with_paper_orders():
    items = [
        WatchItem("AAPL", AssetClass.STOCK, "1Day"),
        WatchItem("AAPL", AssetClass.STOCK, "1Hour"),
    ]
    validate_watchlist(items, orders_enabled=False)
    with pytest.raises(ValueError):
        validate_watchlist(items, orders_enabled=True)


def test_next_run_time_aligns_to_boundary_plus_grace():
    now = datetime(2026, 1, 5, 14, 7, 10, tzinfo=UTC)
    assert next_run_time(now, timedelta(minutes=15)) == datetime(2026, 1, 5, 14, 15, 20, tzinfo=UTC)


class ExplodingBroker(FakeBroker):
    def get_bars(self, symbol, asset_class, timeframe, limit):
        if symbol == "BAD":
            raise ConnectionError("alpaca timeout")
        return super().get_bars(symbol, asset_class, timeframe, limit)


def test_cycle_isolates_errors_and_rate_limits_error_alerts(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    storage.initialize()
    bars = crossover_bars()
    now = after_close(bars)
    notifier = RecordingNotifier()
    watcher = Watcher(
        broker=ExplodingBroker(bars),
        storage=storage,
        notifier=notifier,
        items=[WatchItem("BAD", AssetClass.STOCK, "1Day"), WatchItem("GOOD", AssetClass.STOCK, "1Day")],
        orders_enabled=False,
        clock=lambda: now,
    )

    first = watcher.run_cycle()
    second = watcher.run_cycle()

    assert first["BAD 1Day (stock)"] == "error"
    assert first["GOOD 1Day (stock)"] == "enter_long"
    assert second["GOOD 1Day (stock)"] == "already_processed"
    error_alerts = [m for m in notifier.messages if "Pipeline error" in m]
    assert len(error_alerts) == 1  # cooldown suppresses the repeat


def test_run_forever_sends_start_and_stop(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    storage.initialize()
    bars = crossover_bars()
    notifier = RecordingNotifier()
    watcher = Watcher(
        FakeBroker(bars),
        storage,
        notifier,
        [WatchItem("GOOD", AssetClass.STOCK, "1Day")],
        orders_enabled=False,
        clock=lambda: after_close(bars),
        sleep=lambda seconds: None,
    )
    watcher.run_forever(max_cycles=2)
    assert notifier.messages[0].startswith("▶️ Watcher started (alerts only)")
    assert notifier.messages[-1] == "⏹️ Watcher stopped."
    assert sum("BUY GOOD" in m for m in notifier.messages) == 1


def test_watcher_survives_storage_errors(tmp_path, monkeypatch):
    import sqlite3

    storage = Storage(tmp_path / "db.sqlite3")
    storage.initialize()
    bars = crossover_bars()
    notifier = RecordingNotifier()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "pending_notifications", locked)
    monkeypatch.setattr(storage, "record_run", locked)
    watcher = Watcher(
        FakeBroker(bars), storage, notifier, [WatchItem("GOOD", AssetClass.STOCK, "1Day")],
        orders_enabled=False, clock=lambda: after_close(bars), sleep=lambda seconds: None,
    )
    watcher.run_forever(max_cycles=3)  # must not raise
    assert notifier.messages[-1] == "⏹️ Watcher stopped."


def test_watchlist_strategy_defaults_and_per_entry_override(tmp_path, monkeypatch):
    from trading_pipeline import strategies
    from trading_pipeline.strategies import SmaRsiStrategy

    monkeypatch.setitem(strategies.STRATEGIES, "fast_sma", lambda: SmaRsiStrategy(fast=10, slow=30))
    path = tmp_path / "watchlist.toml"
    path.write_text(
        """
[defaults]
strategy = "fast_sma"

[[watch]]
symbol = "AAPL"

[[watch]]
symbol = "MSFT"
strategy = "sma_rsi"
""",
        encoding="utf-8",
    )
    items = load_watchlist(path, default_limit=250, default_notional=100)
    assert [item.strategy for item in items] == ["fast_sma", "sma_rsi"]


def test_watchlist_rejects_unknown_strategy(tmp_path):
    path = tmp_path / "watchlist.toml"
    path.write_text('[[watch]]\nsymbol = "AAPL"\nstrategy = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown strategy 'nope'"):
        load_watchlist(path, default_limit=250, default_notional=100)


def test_limit_below_strategy_min_bars_is_rejected():
    with pytest.raises(ValueError, match="needs"):
        validate_watchlist([WatchItem("AAPL", AssetClass.STOCK, "1Day", limit=50)], orders_enabled=False)


def test_next_run_time_waits_for_delayed_data():
    delay = timedelta(minutes=16)
    # The 14:00 bar becomes available at 14:16; a 14:07 wake-up would find nothing.
    now = datetime(2026, 1, 5, 14, 7, 10, tzinfo=UTC)
    assert next_run_time(now, timedelta(hours=1), delay) == datetime(2026, 1, 5, 14, 16, 20, tzinfo=UTC)
    after = datetime(2026, 1, 5, 14, 16, 30, tzinfo=UTC)
    assert next_run_time(after, timedelta(hours=1), delay) == datetime(2026, 1, 5, 15, 16, 20, tzinfo=UTC)


def test_watchlist_params_merge_defaults_for_the_default_strategy_only(tmp_path):
    path = tmp_path / "watchlist.toml"
    path.write_text(
        """
[defaults]
strategy = "sma_rsi"
params = { fast = 10, slow = 30 }

[[watch]]
symbol = "AAPL"
params = { slow = 40 }

[[watch]]
symbol = "MSFT"
""",
        encoding="utf-8",
    )
    aapl, msft = load_watchlist(path, default_limit=250, default_notional=100)
    assert aapl.strategy_params == {"fast": 10, "slow": 40}
    assert msft.strategy_params == {"fast": 10, "slow": 30}
    assert aapl.build_strategy().slow == 40


def test_watchlist_rejects_unknown_params(tmp_path):
    path = tmp_path / "watchlist.toml"
    path.write_text('[[watch]]\nsymbol = "AAPL"\nparams = { nope = 1 }\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no parameter 'nope'"):
        load_watchlist(path, default_limit=250, default_notional=100)
