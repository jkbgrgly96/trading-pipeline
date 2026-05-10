from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    asset_class: AssetClass
    action: SignalAction
    confidence: float
    reason: str
    latest_close: float
    timestamp: datetime
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def is_actionable(self) -> bool:
        return self.action in {SignalAction.BUY, SignalAction.SELL}
