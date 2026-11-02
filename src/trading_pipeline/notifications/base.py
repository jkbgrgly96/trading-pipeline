from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from trading_pipeline.domain import Signal

logger = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    pass


class Notifier(ABC):
    @abstractmethod
    def send_message(self, text: str) -> None:
        """Deliver ``text``; raise on failure so the outbox can retry."""
        raise NotImplementedError

    def send(self, signal: Signal) -> None:
        from trading_pipeline.notifications.formatting import format_signal

        self.send_message(format_signal(signal))


class MultiNotifier(Notifier):
    """Send to every notifier; raise if any *required* one failed."""

    def __init__(self, notifiers: list[Notifier], optional: list[Notifier] | None = None) -> None:
        self._required = notifiers
        self._optional = optional or []

    def send_message(self, text: str) -> None:
        for notifier in self._optional:
            try:
                notifier.send_message(text)
            except Exception:  # noqa: BLE001 - optional channels never block alerts
                logger.exception("Optional notifier %s failed", type(notifier).__name__)
        errors = []
        for notifier in self._required:
            try:
                notifier.send_message(text)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(notifier).__name__}: {exc}")
        if errors:
            raise NotificationError("; ".join(errors))
