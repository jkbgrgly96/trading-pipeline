from __future__ import annotations

from trading_pipeline.candles import detect_patterns
from trading_pipeline.domain import AssetClass, Bar, Signal, SignalAction
from trading_pipeline.indicators import rsi, sma


def generate_signal(symbol: str, asset_class: AssetClass, bars: list[Bar]) -> Signal:
    if len(bars) < 60:
        raise ValueError("At least 60 bars are required for the default strategy.")

    closes = [bar.close for bar in bars]
    fast = sma(closes, 20)
    slow = sma(closes, 50)
    rsi_values = rsi(closes, 14)
    patterns = detect_patterns(bars)
    bullish_patterns = [
        pattern for pattern in patterns if pattern.startswith("bullish_")
    ]
    bearish_patterns = [
        pattern for pattern in patterns if pattern.startswith("bearish_")
    ]

    latest = bars[-1]
    previous_fast = fast[-2]
    previous_slow = slow[-2]
    latest_fast = fast[-1]
    latest_slow = slow[-1]
    latest_rsi = rsi_values[-1]

    if None in {previous_fast, previous_slow, latest_fast, latest_slow, latest_rsi}:
        return _hold(symbol, asset_class, latest, "insufficient_indicator_data")

    assert previous_fast is not None
    assert previous_slow is not None
    assert latest_fast is not None
    assert latest_slow is not None
    assert latest_rsi is not None

    bullish_cross = previous_fast <= previous_slow and latest_fast > latest_slow
    bearish_cross = previous_fast >= previous_slow and latest_fast < latest_slow

    if bullish_cross and latest_rsi < 70:
        confidence = 0.65
        reason_parts = ["sma20_crossed_above_sma50", f"rsi={latest_rsi:.1f}"]
        if bullish_patterns:
            confidence += 0.1
            reason_parts.append(f"bullish_candle_pattern={bullish_patterns[0]}")
        return Signal(
            symbol=symbol,
            asset_class=asset_class,
            action=SignalAction.BUY,
            confidence=min(confidence, 0.95),
            reason=", ".join(reason_parts),
            latest_close=latest.close,
            timestamp=latest.timestamp,
            stop_loss=latest.close * 0.96,
            take_profit=latest.close * 1.08,
        )

    if bearish_cross or latest_rsi > 75:
        confidence = 0.6
        reason_parts = ["bearish_or_overbought"]
        if bearish_cross:
            reason_parts.append("sma20_crossed_below_sma50")
        reason_parts.append(f"rsi={latest_rsi:.1f}")
        if bearish_patterns:
            confidence += 0.1
            reason_parts.append(f"bearish_candle_pattern={bearish_patterns[0]}")
        return Signal(
            symbol=symbol,
            asset_class=asset_class,
            action=SignalAction.SELL,
            confidence=min(confidence, 0.95),
            reason=", ".join(reason_parts),
            latest_close=latest.close,
            timestamp=latest.timestamp,
        )

    pattern_text = ",".join(patterns) if patterns else "none"
    return _hold(
        symbol,
        asset_class,
        latest,
        f"no_actionable_setup, rsi={latest_rsi:.1f}, patterns={pattern_text}",
    )


def _hold(symbol: str, asset_class: AssetClass, latest: Bar, reason: str) -> Signal:
    return Signal(
        symbol=symbol,
        asset_class=asset_class,
        action=SignalAction.HOLD,
        confidence=0.0,
        reason=reason,
        latest_close=latest.close,
        timestamp=latest.timestamp,
    )
