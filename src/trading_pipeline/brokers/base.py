from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading_pipeline.domain import AssetClass, Bar


class LiveTradingRefusedError(RuntimeError):
    """Raised when an order would go to a non-paper (real money) account."""


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: float
    avg_entry_price: float


# Order states after which the order can no longer fill. (Alpaca's "stopped"
# means a fill is guaranteed, so it is deliberately not here.)
DEAD_ORDER_STATUSES = frozenset({"canceled", "expired", "rejected", "suspended"})


def is_order_open(status: str | None) -> bool:
    """True while an order may still (partially) fill."""
    return status is not None and status != "filled" and status not in DEAD_ORDER_STATUSES


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    status: str  # lower-case, e.g. ``new``, ``filled``, ``canceled``
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: datetime | None = None

    @property
    def has_fill(self) -> bool:
        return self.filled_qty > 0 and self.filled_avg_price is not None

    @property
    def fill_is_final(self) -> bool:
        """Filled, or (partially) filled and then dead: the fill will not change."""
        return self.has_fill and (self.status == "filled" or self.status in DEAD_ORDER_STATUSES)


class Broker(ABC):
    @property
    @abstractmethod
    def is_paper(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        limit: int,
    ) -> list[Bar]:
        """The latest ``limit`` bars, oldest first (the newest may still be forming)."""
        raise NotImplementedError

    @abstractmethod
    def get_bars_between(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Every bar starting in ``[start, end)``, oldest first (for backtests)."""
        raise NotImplementedError

    @abstractmethod
    def get_position(self, symbol: str, asset_class: AssetClass) -> BrokerPosition | None:
        """Open long position for ``symbol`` or ``None``."""
        raise NotImplementedError

    @abstractmethod
    def submit_market_buy(self, symbol: str, asset_class: AssetClass, notional: float) -> str:
        """Submit a notional market buy and return the order id."""
        raise NotImplementedError

    @abstractmethod
    def close_position(self, symbol: str, asset_class: AssetClass) -> str | None:
        """Close the whole position; return the order id, or ``None`` if flat."""
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order (no-op if it already finished)."""
        raise NotImplementedError

    @abstractmethod
    def get_order(self, order_id: str) -> BrokerOrder:
        """Status and fill details of an order."""
        raise NotImplementedError

    def get_order_status(self, order_id: str) -> str:
        """Lower-case order status, e.g. ``new``, ``filled``, ``canceled``."""
        return self.get_order(order_id).status

    def data_delay(self, asset_class: AssetClass) -> timedelta:
        """How long after a bar closes its data can be fetched (on top of publishing)."""
        return timedelta(0)

    def ensure_paper(self) -> None:
        if not self.is_paper:
            raise LiveTradingRefusedError(
                "Refusing to place an order: broker is not connected to a paper account."
            )
