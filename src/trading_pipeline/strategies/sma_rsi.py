from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from trading_pipeline.domain import AssetClass, Signal, SignalAction
from trading_pipeline.features import FeatureSnapshot, IndicatorDefinition
from trading_pipeline.strategies.base import History, Strategy, hold_signal


@dataclass(frozen=True)
class SmaRsiStrategy(Strategy):
    """SMA fast/slow crossover with an RSI filter; candle patterns add confidence.

    BUY: fast SMA crosses above slow SMA while RSI < ``rsi_buy_max``.
    SELL: fast SMA crosses below slow SMA, or RSI > ``rsi_overbought``.
    """

    name: ClassVar[str] = "sma_rsi"

    fast: int = 20
    slow: int = 50
    rsi_length: int = 14
    rsi_buy_max: float = 70
    rsi_overbought: float = 75
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.08

    def __post_init__(self) -> None:
        if not 0 < self.fast < self.slow:
            raise ValueError("sma_rsi needs 0 < fast < slow.")
        if not (0 < self.stop_loss_pct < 1 and self.take_profit_pct > 0):
            raise ValueError("sma_rsi needs 0 < stop_loss_pct < 1 and take_profit_pct > 0.")

    @property
    def _fast_name(self) -> str:
        return f"sma_{self.fast}"

    @property
    def _slow_name(self) -> str:
        return f"sma_{self.slow}"

    @property
    def _rsi_name(self) -> str:
        return f"rsi_{self.rsi_length}"

    @property
    def features(self) -> list[IndicatorDefinition]:
        return [
            IndicatorDefinition(self._fast_name, "sma", {"length": self.fast}),
            IndicatorDefinition(self._slow_name, "sma", {"length": self.slow}),
            IndicatorDefinition(self._rsi_name, "rsi", {"length": self.rsi_length}),
        ]

    @property
    def min_bars(self) -> int:
        # The slow SMA needs ``slow`` bars, plus the previous bar for the
        # crossover and some warm-up for RSI smoothing.
        return max(self.slow, self.rsi_length) + 10

    def risk_levels(
        self, entry_price: float, latest: FeatureSnapshot
    ) -> tuple[float | None, float | None]:
        return entry_price * (1 - self.stop_loss_pct), entry_price * (1 + self.take_profit_pct)

    def signal(self, symbol: str, asset_class: AssetClass, history: History) -> Signal:
        latest = history.latest
        if len(history) < 2:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
        previous = history.previous

        prev_fast, prev_slow = previous.get(self._fast_name), previous.get(self._slow_name)
        fast, slow = latest.get(self._fast_name), latest.get(self._slow_name)
        rsi = latest.get(self._rsi_name)
        if prev_fast is None or prev_slow is None or fast is None or slow is None or rsi is None:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")

        bullish_patterns = [p for p in latest.patterns if p.startswith("bullish_")]
        bearish_patterns = [p for p in latest.patterns if p.startswith("bearish_")]
        bullish_cross = prev_fast <= prev_slow and fast > slow
        bearish_cross = prev_fast >= prev_slow and fast < slow

        if bullish_cross and rsi < self.rsi_buy_max:
            confidence = 0.65
            reason_parts = [f"{self._fast_name}_crossed_above_{self._slow_name}", f"rsi={rsi:.1f}"]
            if bullish_patterns:
                confidence += 0.1
                reason_parts.append(f"bullish_candle_pattern={bullish_patterns[0]}")
            stop_loss, take_profit = self.risk_levels(latest.close, latest)
            return Signal(
                symbol=symbol,
                asset_class=asset_class,
                action=SignalAction.BUY,
                confidence=min(confidence, 0.95),
                reason=", ".join(reason_parts),
                latest_close=latest.close,
                timestamp=latest.timestamp,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        if bearish_cross or rsi > self.rsi_overbought:
            confidence = 0.6
            reason_parts = ["bearish_or_overbought"]
            if bearish_cross:
                reason_parts.append(f"{self._fast_name}_crossed_below_{self._slow_name}")
            reason_parts.append(f"rsi={rsi:.1f}")
            if bearish_patterns:
                confidence += 0.1
                reason_parts.append(f"bearish_candle_pattern={bearish_patterns[0]}")
            return Signal(
                symbol=symbol,
                asset_class=asset_class,
                action=SignalAction.SELL,
                confidence=min(confidence, 0.95),
                reason=", ".join(reason_parts),
                latest_close=latest.close,
                timestamp=latest.timestamp,
            )

        pattern_text = ",".join(latest.patterns) if latest.patterns else "none"
        return hold_signal(
            symbol,
            asset_class,
            latest,
            f"no_actionable_setup, rsi={rsi:.1f}, patterns={pattern_text}",
        )
