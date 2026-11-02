"""Buy short, sharp dips inside a long-term uptrend (Connors-style RSI(2)).

BUY: close above its ``trend_length`` SMA and RSI(``rsi_length``) below
``entry_rsi``. SELL: close above its ``exit_sma_length`` SMA. Optional: a
maximum holding period and an ATR stop (the classic version has no stop; a
wide one keeps risk-based sizing meaningful).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from trading_pipeline.domain import AssetClass, OpenPosition, PositionPlan, Signal, SignalAction
from trading_pipeline.features import FeatureSnapshot, IndicatorDefinition
from trading_pipeline.strategies.base import History, Strategy, hold_signal


@dataclass(frozen=True)
class Rsi2PullbackStrategy(Strategy):
    name: ClassVar[str] = "rsi2_pullback"

    rsi_length: int = 2
    entry_rsi: float = 10.0
    trend_length: int = 200
    exit_sma_length: int = 5
    max_hold_bars: int = 0  # 0: no time exit
    stop_atr_length: int = 14
    stop_atr_mult: float = 3.0  # 0: no stop
    stop_trigger: str = "low"  # "low" | "close"

    def __post_init__(self) -> None:
        if min(self.rsi_length, self.trend_length, self.exit_sma_length, self.stop_atr_length) < 1:
            raise ValueError("rsi2_pullback: lengths must be positive.")
        if not 0 < self.entry_rsi < 100:
            raise ValueError("rsi2_pullback: entry_rsi must be in (0, 100).")
        if self.max_hold_bars < 0 or self.stop_atr_mult < 0:
            raise ValueError("rsi2_pullback: max_hold_bars and stop_atr_mult must be >= 0.")
        if self.stop_trigger not in {"low", "close"}:
            raise ValueError("rsi2_pullback: stop_trigger must be 'low' or 'close'.")

    @property
    def _rsi(self) -> str:
        return f"rsi_{self.rsi_length}"

    @property
    def _trend(self) -> str:
        return f"sma_{self.trend_length}"

    @property
    def _exit(self) -> str:
        return f"sma_{self.exit_sma_length}"

    @property
    def _atr(self) -> str:
        return f"atr_{self.stop_atr_length}"

    @property
    def features(self) -> list[IndicatorDefinition]:
        definitions = [
            IndicatorDefinition(self._rsi, "rsi", {"length": self.rsi_length}),
            IndicatorDefinition(self._trend, "sma", {"length": self.trend_length}),
            IndicatorDefinition(self._atr, "atr", {"length": self.stop_atr_length}),
        ]
        if self.exit_sma_length != self.trend_length:
            definitions.append(IndicatorDefinition(self._exit, "sma", {"length": self.exit_sma_length}))
        return definitions

    @property
    def min_bars(self) -> int:
        return max(self.trend_length, self.exit_sma_length, self.rsi_length + 1, self.stop_atr_length + 1) + 5

    def signal(self, symbol: str, asset_class: AssetClass, history: History) -> Signal:
        latest = history.latest
        rsi, trend, exit_sma = latest.get(self._rsi), latest.get(self._trend), latest.get(self._exit)
        if rsi is None or trend is None or exit_sma is None:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
        state = f"rsi{self.rsi_length}={rsi:.1f}, close={latest.close:.2f}, sma{self.exit_sma_length}={exit_sma:.2f}"
        if latest.close > exit_sma:
            return Signal(
                symbol, asset_class, SignalAction.SELL, 0.6, f"close_above_exit_sma, {state}",
                latest.close, latest.timestamp,
            )
        if latest.close > trend and rsi < self.entry_rsi:
            stop_loss, take_profit = self.risk_levels(latest.close, latest)
            return Signal(
                symbol, asset_class, SignalAction.BUY, 0.6, f"pullback_in_uptrend, {state}",
                latest.close, latest.timestamp, stop_loss, take_profit,
            )
        return hold_signal(symbol, asset_class, latest, f"no_setup, {state}")

    def risk_levels(self, entry_price: float, latest: FeatureSnapshot) -> tuple[float | None, float | None]:
        atr = latest.get(self._atr)
        if self.stop_atr_mult <= 0 or atr is None:
            return None, None
        return entry_price - self.stop_atr_mult * atr, None

    def manage(self, position: OpenPosition, history: History) -> PositionPlan | None:
        if self.max_hold_bars and len(history.since(position.entry_time)) >= self.max_hold_bars:
            return PositionPlan(exit_reason=f"max_holding_period ({self.max_hold_bars} bars)")
        return None
