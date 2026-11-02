"""Donchian channel breakout (Turtle-style trend following).

BUY: close above the highest high of the previous ``entry_length`` bars.
SELL: close below the lowest low of the previous ``exit_length`` bars.
Initial stop: ``stop_atr_mult`` x ATR(``atr_length``) below the entry.
The channels come from pandas-ta-classic; they include the current bar, so
the rules compare the close with the *previous* bar's channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from trading_pipeline.domain import AssetClass, Signal, SignalAction
from trading_pipeline.features import FeatureSnapshot, IndicatorDefinition
from trading_pipeline.strategies.base import History, Strategy, hold_signal


@dataclass(frozen=True)
class DonchianBreakoutStrategy(Strategy):
    name: ClassVar[str] = "donchian_breakout"

    entry_length: int = 55
    exit_length: int = 20
    atr_length: int = 20
    stop_atr_mult: float = 2.0
    stop_trigger: str = "low"  # "low" | "close"

    def __post_init__(self) -> None:
        if not 0 < self.exit_length < self.entry_length:
            raise ValueError("donchian_breakout: needs 0 < exit_length < entry_length.")
        if self.atr_length < 1 or self.stop_atr_mult <= 0:
            raise ValueError("donchian_breakout: atr_length and stop_atr_mult must be positive.")
        if self.stop_trigger not in {"low", "close"}:
            raise ValueError("donchian_breakout: stop_trigger must be 'low' or 'close'.")

    @property
    def _channel(self) -> str:
        return f"dc_{self.entry_length}_{self.exit_length}"

    @property
    def _atr(self) -> str:
        return f"atr_{self.atr_length}"

    @property
    def features(self) -> list[IndicatorDefinition]:
        return [
            IndicatorDefinition(
                self._channel, "donchian",
                {"upper_length": self.entry_length, "lower_length": self.exit_length},
            ),
            IndicatorDefinition(self._atr, "atr", {"length": self.atr_length}),
        ]

    @property
    def min_bars(self) -> int:
        return max(self.entry_length, self.atr_length + 1) + 2

    def signal(self, symbol: str, asset_class: AssetClass, history: History) -> Signal:
        latest = history.latest
        if len(history) < 2:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
        previous = history.previous
        upper, lower = previous.get(f"{self._channel}_upper"), previous.get(f"{self._channel}_lower")
        if upper is None or lower is None:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
        state = f"close={latest.close:.2f}, high{self.entry_length}={upper:.2f}, low{self.exit_length}={lower:.2f}"
        if latest.close < lower:
            return Signal(
                symbol, asset_class, SignalAction.SELL, 0.6, f"channel_breakdown, {state}",
                latest.close, latest.timestamp,
            )
        if latest.close > upper:
            stop_loss, take_profit = self.risk_levels(latest.close, latest)
            return Signal(
                symbol, asset_class, SignalAction.BUY, 0.6, f"channel_breakout, {state}",
                latest.close, latest.timestamp, stop_loss, take_profit,
            )
        return hold_signal(symbol, asset_class, latest, f"inside_channel, {state}")

    def risk_levels(self, entry_price: float, latest: FeatureSnapshot) -> tuple[float | None, float | None]:
        atr = latest.get(self._atr)
        if atr is None:
            return None, None
        return entry_price - self.stop_atr_mult * atr, None
