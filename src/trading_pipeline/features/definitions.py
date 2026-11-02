from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IndicatorDefinition:
    name: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


# Always computed, whatever the strategy: the dashboard chart draws these.
DEFAULT_FEATURES = [
    IndicatorDefinition("sma_20", "sma", {"length": 20}),
    IndicatorDefinition("sma_50", "sma", {"length": 50}),
    IndicatorDefinition("rsi_14", "rsi", {"length": 14}),
    IndicatorDefinition("macd", "macd", {"fast": 12, "slow": 26, "signal": 9}),
    IndicatorDefinition("bbands_20_2", "bbands", {"length": 20, "std": 2.0}),
    IndicatorDefinition("atr_14", "atr", {"length": 14}),
    IndicatorDefinition("candles", "candles", {"patterns": "all"}),
]


def merge_definitions(*groups: Iterable[IndicatorDefinition]) -> list[IndicatorDefinition]:
    """Concatenate definition lists, dropping exact duplicates by name.

    Two definitions with the same name but different settings would write the
    same column twice, so that is rejected instead of silently picking one.
    """
    merged: dict[str, IndicatorDefinition] = {}
    for group in groups:
        for definition in group:
            existing = merged.get(definition.name)
            if existing is None:
                merged[definition.name] = definition
            elif existing != definition:
                raise ValueError(
                    f"Feature {definition.name!r} is defined twice with different settings: "
                    f"{existing} vs {definition}"
                )
    return list(merged.values())
