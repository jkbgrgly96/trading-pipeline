"""How many dollars an entry buys. Shared by paper orders, alerts and backtests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SizingMode(str, Enum):
    NOTIONAL = "notional"  # a fixed USD amount per entry
    RISK = "risk"  # the amount that loses ``risk_per_trade`` if the stop is hit


@dataclass(frozen=True)
class Sizing:
    mode: SizingMode = SizingMode.NOTIONAL
    # Fixed USD per entry; in risk mode, used when an entry has no stop.
    notional: float = 100.0
    risk_per_trade: float = 10.0
    max_notional: float = 1_000.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", SizingMode(self.mode))
        if self.notional <= 0 or self.risk_per_trade <= 0 or self.max_notional <= 0:
            raise ValueError("notional, risk_per_trade and max_notional must be positive.")

    def entry_notional(self, price: float, stop: float | None) -> float:
        """USD to buy at ``price`` with a stop at ``stop``.

        Risk mode: shares = risk / (price - stop), so the notional is
        risk * price / (price - stop), capped at ``max_notional``. Without a
        usable stop (none, or not below the price) it falls back to ``notional``.
        """
        if self.mode == SizingMode.NOTIONAL or stop is None or not 0 < stop < price:
            return self.notional
        return min(self.max_notional, self.risk_per_trade * price / (price - stop))

    def describe(self, price: float, stop: float | None) -> str:
        amount = self.entry_notional(price, stop)
        if self.mode == SizingMode.RISK and stop is not None and 0 < stop < price:
            risk = amount * (price - stop) / price
            capped = " (capped)" if amount >= self.max_notional else ""
            return f"${amount:,.2f}{capped}, risking ${risk:,.2f} to the stop"
        return f"${amount:,.2f}"
