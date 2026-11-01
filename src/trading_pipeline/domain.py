from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


class SignalAction(str, Enum):
    """What the strategy thinks about the market on the latest closed bar."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class DecisionAction(str, Enum):
    """What you should do, given the signal *and* your current position.

    This is what notifications and orders are driven by. A SELL signal with
    no open position, or a BUY signal while already long, is ``NONE``.
    """

    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    NONE = "none"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss_hit"
    TAKE_PROFIT = "take_profit_hit"
    SIGNAL = "sell_signal"


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


@dataclass(frozen=True)
class OpenPosition:
    """A long position as the pipeline sees it (virtual or synced from paper)."""

    symbol: str
    asset_class: AssetClass
    timeframe: str
    entry_price: float
    entry_time: datetime
    quantity: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    source: str = "virtual"
    id: int | None = None
    entry_order_id: str | None = None


@dataclass(frozen=True)
class Decision:
    action: DecisionAction
    reason: str
    signal: Signal
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    exit_reason: ExitReason | None = None
    position: OpenPosition | None = None

    @property
    def is_actionable(self) -> bool:
        return self.action != DecisionAction.NONE
