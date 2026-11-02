from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from trading_pipeline.brokers.base import Broker, BrokerPosition
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.notifications.base import Notifier

START = datetime(2026, 1, 1, tzinfo=UTC)
BUY_BAR_INDEX = 97  # first bar where the synthetic series below crosses up


def crossover_bars(count: int = BUY_BAR_INDEX + 1) -> list[Bar]:
    """Daily bars: a downtrend, then a gentle recovery whose SMA20/50 cross
    happens on bar 97 with RSI < 70 (a real BUY signal, no mocking)."""
    closes = [100 - 0.3 * i + 2 * math.sin(i / 3) for i in range(70)]
    closes += [79 + 0.18 * j + 2.5 * math.sin(j / 2) for j in range(1, 80)]
    return [
        Bar(START + timedelta(days=i), c - 0.2, c + 0.8, c - 0.8, c, 1_000)
        for i, c in enumerate(closes[:count])
    ]


def after_close(bars: list[Bar], timeframe: timedelta = timedelta(days=1)) -> datetime:
    """A 'now' just after the last bar has closed."""
    return bars[-1].timestamp + timeframe + timedelta(minutes=1)


def crash_bar(previous: Bar, drop: float = 0.10) -> Bar:
    price = previous.close * (1 - drop)
    return Bar(previous.timestamp + timedelta(days=1), previous.close, previous.close, price, price, 5_000)


class FakeBroker(Broker):
    def __init__(self, bars: list[Bar], paper: bool = True) -> None:
        self.bars = bars
        self._paper = paper
        self.positions: dict[str, BrokerPosition] = {}
        self.order_statuses: dict[str, str] = {}
        self.buys: list[tuple[str, float]] = []
        self.closes: list[str] = []
        self.canceled: list[str] = []
        self.fail_orders = False
        self._order_seq = 0

    @property
    def is_paper(self) -> bool:
        return self._paper

    def get_bars(self, symbol, asset_class, timeframe, limit):
        return list(self.bars[-limit:])

    def get_position(self, symbol, asset_class):
        return self.positions.get(symbol)

    def submit_market_buy(self, symbol, asset_class: AssetClass, notional):
        self.ensure_paper()
        if self.fail_orders:
            raise RuntimeError("insufficient buying power")
        self._order_seq += 1
        order_id = f"order-{self._order_seq}"
        self.buys.append((symbol, notional))
        self.order_statuses[order_id] = "accepted"
        return order_id

    def close_position(self, symbol, asset_class):
        self.ensure_paper()
        if self.fail_orders:
            raise RuntimeError("market closed")
        if symbol not in self.positions:
            return None
        del self.positions[symbol]
        self.closes.append(symbol)
        self._order_seq += 1
        return f"order-{self._order_seq}"

    def cancel_order(self, order_id):
        self.ensure_paper()
        if self.order_statuses.get(order_id) not in {"filled", None}:
            self.order_statuses[order_id] = "canceled"
        self.canceled.append(order_id)

    def get_order_status(self, order_id):
        return self.order_statuses.get(order_id, "filled")


class RecordingNotifier(Notifier):
    def __init__(self, failures: int = 0) -> None:
        self.messages: list[str] = []
        self.failures = failures

    def send_message(self, text: str) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("telegram down")
        self.messages.append(text)
