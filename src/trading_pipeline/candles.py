from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
import pandas_ta_classic as ta

from trading_pipeline.domain import Bar


def detect_patterns(
    bars: list[Bar],
    pattern_names: str | Sequence[str] = "all",
) -> list[str]:
    if len(bars) < 10:
        return []

    frame = _bars_to_candle_frame(bars)
    result = ta.cdl_pattern(
        frame["open"],
        frame["high"],
        frame["low"],
        frame["close"],
        name=pattern_names,
        fillna=0,
    )
    if result is None or result.empty:
        return []

    latest = result.iloc[-1]
    patterns: list[str] = []
    for column, raw_value in latest.items():
        value = float(raw_value)
        if value == 0:
            continue

        name = _normalize_pattern_name(column)
        direction = "bullish" if value > 0 else "bearish"
        patterns.append(f"{direction}_{name}")

    return patterns


def _bars_to_candle_frame(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
        },
        index=pd.DatetimeIndex([bar.timestamp for bar in bars]),
    ).sort_index()


def _normalize_pattern_name(column: str) -> str:
    normalized = column.lower()
    if normalized.startswith("cdl_"):
        normalized = normalized[4:]
    return normalized
