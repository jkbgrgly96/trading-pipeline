from __future__ import annotations

from abc import ABC, abstractmethod

from trading_pipeline.domain import Signal


class Notifier(ABC):
    @abstractmethod
    def send(self, signal: Signal) -> None:
        raise NotImplementedError
