from __future__ import annotations

from trading_pipeline.domain import Signal
from trading_pipeline.notifications.base import Notifier


class ConsoleNotifier(Notifier):
    def send(self, signal: Signal) -> None:
        print(format_signal(signal))


def format_signal(signal: Signal) -> str:
    lines = [
        f"{signal.symbol}: {signal.action.value.upper()}",
        f"close={signal.latest_close:.4f}",
        f"confidence={signal.confidence:.2f}",
        f"time={signal.timestamp.isoformat()}",
        f"reason={signal.reason}",
    ]
    if signal.stop_loss is not None:
        lines.append(f"stop_loss={signal.stop_loss:.4f}")
    if signal.take_profit is not None:
        lines.append(f"take_profit={signal.take_profit:.4f}")
    return "\n".join(lines)
