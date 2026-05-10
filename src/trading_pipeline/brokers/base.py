from __future__ import annotations

from abc import ABC, abstractmethod

from trading_pipeline.domain import AssetClass, Bar, Signal


class Broker(ABC):
    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        limit: int,
    ) -> list[Bar]:
        raise NotImplementedError

    @abstractmethod
    def submit_signal_order(self, signal: Signal, notional: float) -> str:
        raise NotImplementedError
