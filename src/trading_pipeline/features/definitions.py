from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IndicatorDefinition:
    name: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


DEFAULT_FEATURES = [
    IndicatorDefinition("sma_20", "sma", {"length": 20}),
    IndicatorDefinition("sma_50", "sma", {"length": 50}),
    IndicatorDefinition("rsi_14", "rsi", {"length": 14}),
    IndicatorDefinition("macd", "macd", {"fast": 12, "slow": 26, "signal": 9}),
    IndicatorDefinition("bbands_20_2", "bbands", {"length": 20, "std": 2.0}),
    IndicatorDefinition("atr_14", "atr", {"length": 14}),
    IndicatorDefinition("candles", "candles", {"patterns": "all"}),
]
