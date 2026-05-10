from datetime import UTC, datetime, timedelta

from trading_pipeline.candles import detect_patterns
from trading_pipeline.domain import Bar


def bar(open_: float, high: float, low: float, close: float, index: int = 0) -> Bar:
    return Bar(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def baseline_bars(count: int = 58) -> list[Bar]:
    return [
        bar(
            100 + index * 0.1,
            101 + index * 0.1,
            99 + index * 0.1,
            100.1 + index * 0.1,
            index,
        )
        for index in range(count)
    ]


def test_detects_bullish_engulfing():
    patterns = detect_patterns(
        baseline_bars()
        + [
            bar(100, 101, 97, 98, 58),
            bar(97.5, 102, 97, 101, 59),
        ]
    )
    assert "bullish_engulfing" in patterns


def test_detects_bearish_engulfing():
    patterns = detect_patterns(
        baseline_bars()
        + [
            bar(98, 101, 97, 100, 58),
            bar(100.5, 101, 96, 97.5, 59),
        ]
    )
    assert "bearish_engulfing" in patterns


def test_returns_empty_list_when_no_pattern_is_detected():
    assert detect_patterns(baseline_bars(2)) == []
