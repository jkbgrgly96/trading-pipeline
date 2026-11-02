from __future__ import annotations

import pandas as pd

from trading_pipeline.domain import Bar
from trading_pipeline.features.backends.pandas_ta import compute_pandas_ta_features
from trading_pipeline.features.definitions import DEFAULT_FEATURES, IndicatorDefinition
from trading_pipeline.features.frame import bars_to_ohlcv_frame
from trading_pipeline.features.snapshot import FeatureSnapshot, normalize_numeric


def compute_feature_frame(
    bars: list[Bar],
    definitions: list[IndicatorDefinition] | None = None,
) -> pd.DataFrame:
    return compute_pandas_ta_features(
        bars_to_ohlcv_frame(bars),
        definitions or DEFAULT_FEATURES,
    )


def latest_snapshot(frame: pd.DataFrame) -> FeatureSnapshot:
    return snapshot_from_frame(frame, -1)


OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def snapshot_from_frame(frame: pd.DataFrame, index: int = -1) -> FeatureSnapshot:
    if frame.empty:
        raise ValueError("Cannot build a feature snapshot from an empty frame.")

    row = frame.iloc[index]
    return _snapshot_from_mapping(frame.index[index], row.to_dict())


def snapshots_from_frame(frame: pd.DataFrame) -> list[FeatureSnapshot]:
    """Build a snapshot for every row; much faster than repeated iloc lookups."""
    if frame.empty:
        return []
    records = frame.to_dict(orient="records")
    return [
        _snapshot_from_mapping(timestamp, record)
        for timestamp, record in zip(frame.index, records, strict=True)
    ]


def _snapshot_from_mapping(timestamp, row: dict) -> FeatureSnapshot:
    values: dict[str, float | None] = {}
    patterns: list[str] = []

    for column, raw_value in row.items():
        if column in OHLCV_COLUMNS:
            continue
        if column.startswith("pattern__"):
            value = normalize_numeric(raw_value)
            if value:
                direction = "bullish" if value > 0 else "bearish"
                patterns.append(f"{direction}_{column.removeprefix('pattern__')}")
            continue
        values[column] = normalize_numeric(raw_value)

    return FeatureSnapshot(
        timestamp=pd.Timestamp(timestamp).to_pydatetime(),
        open=float(row["Open"]),
        high=float(row["High"]),
        low=float(row["Low"]),
        close=float(row["Close"]),
        volume=float(row.get("Volume", 0.0)),
        values=values,
        patterns=patterns,
    )
