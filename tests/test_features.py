from datetime import UTC, datetime, timedelta

from trading_pipeline.domain import Bar
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshot_from_frame
from trading_pipeline.features.definitions import IndicatorDefinition
from trading_pipeline.features.frame import bars_to_ohlcv_frame


def make_bars(count: int = 80) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Bar(
            timestamp=start + timedelta(days=index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100 + index,
            volume=1_000 + index,
        )
        for index in range(count)
    ]


def test_bars_to_ohlcv_frame_uses_backtesting_column_names():
    frame = bars_to_ohlcv_frame(make_bars(2))

    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.iloc[-1]["Close"] == 101


def test_compute_feature_frame_adds_normalized_indicator_columns():
    frame = compute_feature_frame(
        make_bars(),
        [
            IndicatorDefinition("sma_20", "sma", {"length": 20}),
            IndicatorDefinition("rsi_14", "rsi", {"length": 14}),
            IndicatorDefinition("macd", "macd", {}),
            IndicatorDefinition("bbands_20_2", "bbands", {"length": 20, "std": 2.0}),
            IndicatorDefinition("atr_14", "atr", {"length": 14}),
        ],
    )

    assert "sma_20" in frame.columns
    assert "rsi_14" in frame.columns
    assert "macd_hist" in frame.columns
    assert "bbands_20_2_upper" in frame.columns
    assert "atr_14" in frame.columns
    assert latest_snapshot(frame).sma_20 is not None


def test_snapshot_extracts_pattern_names_from_feature_frame():
    bars = make_bars(58)
    bars += [
        Bar(datetime(2026, 3, 1, tzinfo=UTC), 100, 101, 97, 98, 100),
        Bar(datetime(2026, 3, 2, tzinfo=UTC), 97.5, 102, 97, 101, 100),
    ]
    frame = compute_feature_frame(
        bars,
        [IndicatorDefinition("candles", "candles", {"patterns": "all"})],
    )

    assert "bullish_engulfing" in snapshot_from_frame(frame).patterns
