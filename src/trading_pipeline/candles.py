from __future__ import annotations

from trading_pipeline.domain import Bar
from trading_pipeline.features.definitions import IndicatorDefinition
from trading_pipeline.features.engine import compute_feature_frame, latest_snapshot


def detect_patterns(
    bars: list[Bar],
    pattern_names: str = "all",
) -> list[str]:
    frame = compute_feature_frame(
        bars,
        [IndicatorDefinition("candles", "candles", {"patterns": pattern_names})],
    )
    return latest_snapshot(frame).patterns
