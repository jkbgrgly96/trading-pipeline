from __future__ import annotations

from trading_pipeline.domain import AssetClass, Bar, Signal, SignalAction
from trading_pipeline.features import (
    FeatureSnapshot,
    compute_feature_frame,
    latest_snapshot,
    snapshot_from_frame,
)


def generate_signal(symbol: str, asset_class: AssetClass, bars: list[Bar]) -> Signal:
    if len(bars) < 60:
        raise ValueError("At least 60 bars are required for the default strategy.")

    frame = compute_feature_frame(bars)
    return generate_signal_from_features(
        symbol=symbol,
        asset_class=asset_class,
        latest=latest_snapshot(frame),
        previous=snapshot_from_frame(frame, -2),
    )


def generate_signal_from_features(
    symbol: str,
    asset_class: AssetClass,
    latest: FeatureSnapshot,
    previous: FeatureSnapshot,
) -> Signal:
    bullish_patterns = [
        pattern for pattern in latest.patterns if pattern.startswith("bullish_")
    ]
    bearish_patterns = [
        pattern for pattern in latest.patterns if pattern.startswith("bearish_")
    ]

    if None in {
        previous.sma_20,
        previous.sma_50,
        latest.sma_20,
        latest.sma_50,
        latest.rsi_14,
    }:
        return _hold(symbol, asset_class, latest, "insufficient_indicator_data")

    assert previous.sma_20 is not None
    assert previous.sma_50 is not None
    assert latest.sma_20 is not None
    assert latest.sma_50 is not None
    assert latest.rsi_14 is not None

    bullish_cross = previous.sma_20 <= previous.sma_50 and latest.sma_20 > latest.sma_50
    bearish_cross = previous.sma_20 >= previous.sma_50 and latest.sma_20 < latest.sma_50

    if bullish_cross and latest.rsi_14 < 70:
        confidence = 0.65
        reason_parts = ["sma20_crossed_above_sma50", f"rsi={latest.rsi_14:.1f}"]
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

    if bearish_cross or latest.rsi_14 > 75:
        confidence = 0.6
        reason_parts = ["bearish_or_overbought"]
        if bearish_cross:
            reason_parts.append("sma20_crossed_below_sma50")
        reason_parts.append(f"rsi={latest.rsi_14:.1f}")
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

    pattern_text = ",".join(latest.patterns) if latest.patterns else "none"
    return _hold(
        symbol,
        asset_class,
        latest,
        f"no_actionable_setup, rsi={latest.rsi_14:.1f}, patterns={pattern_text}",
    )


def _hold(
    symbol: str,
    asset_class: AssetClass,
    latest: FeatureSnapshot,
    reason: str,
) -> Signal:
    return Signal(
        symbol=symbol,
        asset_class=asset_class,
        action=SignalAction.HOLD,
        confidence=0.0,
        reason=reason,
        latest_close=latest.close,
        timestamp=latest.timestamp,
    )
