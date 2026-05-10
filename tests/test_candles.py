from datetime import UTC, datetime

from trading_pipeline.candles import detect_patterns
from trading_pipeline.domain import Bar


def bar(open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def test_detects_bullish_engulfing():
    patterns = detect_patterns([
        bar(10, 11, 8, 9),
        bar(8.5, 11.5, 8, 10.5),
    ])
    assert "bullish_engulfing" in patterns


def test_detects_bearish_engulfing():
    patterns = detect_patterns([
        bar(9, 11, 8, 10),
        bar(10.5, 11, 8, 8.5),
    ])
    assert "bearish_engulfing" in patterns
