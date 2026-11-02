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


def test_new_indicator_kinds_produce_named_columns():
    import pandas_ta_classic as ta

    bars = make_bars(120)
    frame = compute_feature_frame(
        bars,
        [
            IndicatorDefinition("ema_10", "ema", {"length": 10}),
            IndicatorDefinition("adx_14", "adx", {"length": 14}),
            IndicatorDefinition("dc", "donchian", {"lower_length": 20, "upper_length": 55}),
            IndicatorDefinition("stoch", "stoch", {"k": 14, "d": 3, "smooth_k": 3}),
            IndicatorDefinition("natr_14", "natr", {"length": 14}),
            IndicatorDefinition("rvol_20", "rvol", {"length": 20}),
            IndicatorDefinition("roc_10", "roc", {"length": 10}),
        ],
    )
    last = frame.iloc[-1]
    for column in [
        "ema_10", "adx_14", "adx_14_dmp", "adx_14_dmn", "dc_lower", "dc_middle", "dc_upper",
        "stoch_k", "stoch_d", "natr_14", "rvol_20", "roc_10",
    ]:
        assert column in frame.columns and last[column] == last[column], column  # present, not NaN

    ohlcv = bars_to_ohlcv_frame(bars)
    assert last["ema_10"] == ta.ema(ohlcv["Close"], length=10).iloc[-1]
    assert last["dc_upper"] == ohlcv["High"].iloc[-55:].max()  # includes the current bar
    assert last["dc_lower"] == ohlcv["Low"].iloc[-20:].min()
    assert last["rvol_20"] == ohlcv["Volume"].iloc[-1] / ohlcv["Volume"].iloc[-20:].mean()
    assert 0 <= last["stoch_k"] <= 100
    assert last["adx_14"] > 50  # a steady uptrend is a strong trend
    assert last["adx_14_dmp"] > last["adx_14_dmn"]


def test_relative_volume_is_nan_when_average_volume_is_zero():
    bars = [Bar(b.timestamp, b.open, b.high, b.low, b.close, 0) for b in make_bars(30)]
    frame = compute_feature_frame(bars, [IndicatorDefinition("rvol", "rvol", {"length": 5})])
    assert frame["rvol"].iloc[-1] != frame["rvol"].iloc[-1]
