from __future__ import annotations

from trading_pipeline.domain import Bar


def detect_patterns(bars: list[Bar]) -> list[str]:
    if len(bars) < 2:
        return []

    previous = bars[-2]
    current = bars[-1]
    patterns: list[str] = []

    if _bullish_engulfing(previous, current):
        patterns.append("bullish_engulfing")
    if _bearish_engulfing(previous, current):
        patterns.append("bearish_engulfing")
    if _hammer(current):
        patterns.append("hammer")
    if _shooting_star(current):
        patterns.append("shooting_star")

    return patterns


def _body_size(bar: Bar) -> float:
    return abs(bar.close - bar.open)


def _range_size(bar: Bar) -> float:
    return max(bar.high - bar.low, 0.00000001)


def _bullish_engulfing(previous: Bar, current: Bar) -> bool:
    return (
        previous.close < previous.open
        and current.close > current.open
        and current.open <= previous.close
        and current.close >= previous.open
    )


def _bearish_engulfing(previous: Bar, current: Bar) -> bool:
    return (
        previous.close > previous.open
        and current.close < current.open
        and current.open >= previous.close
        and current.close <= previous.open
    )


def _hammer(bar: Bar) -> bool:
    body = _body_size(bar)
    candle_range = _range_size(bar)
    lower_wick = min(bar.open, bar.close) - bar.low
    upper_wick = bar.high - max(bar.open, bar.close)
    return body / candle_range <= 0.35 and lower_wick >= body * 2 and upper_wick <= body


def _shooting_star(bar: Bar) -> bool:
    body = _body_size(bar)
    candle_range = _range_size(bar)
    lower_wick = min(bar.open, bar.close) - bar.low
    upper_wick = bar.high - max(bar.open, bar.close)
    return body / candle_range <= 0.35 and upper_wick >= body * 2 and lower_wick <= body
