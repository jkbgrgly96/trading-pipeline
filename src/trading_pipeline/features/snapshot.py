from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isnan
from typing import Any


@dataclass(frozen=True)
class FeatureSnapshot:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    values: dict[str, float | None]
    patterns: list[str]

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    @property
    def sma_20(self) -> float | None:
        return self.get("sma_20")

    @property
    def sma_50(self) -> float | None:
        return self.get("sma_50")

    @property
    def rsi_14(self) -> float | None:
        return self.get("rsi_14")

    @property
    def macd(self) -> float | None:
        return self.get("macd")

    @property
    def macd_hist(self) -> float | None:
        return self.get("macd_hist")

    @property
    def macd_signal(self) -> float | None:
        return self.get("macd_signal")

    @property
    def atr_14(self) -> float | None:
        return self.get("atr_14")


def normalize_numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    if isnan(normalized):
        return None
    return normalized
