from __future__ import annotations

from trading_pipeline.domain import AssetClass, Bar, Signal, SignalAction
from trading_pipeline.features import (
    FeatureSnapshot,
    compute_feature_frame,
    latest_snapshot,
    snapshot_from_frame,
)

# SMA 50 needs 50 bars, plus the previous bar for the crossover and some
# warm-up for RSI smoothing.
MIN_BARS = 60
STOP_LOSS_PCT = 0.04
TAKE_PROFIT_PCT = 0.08
RSI_BUY_MAX = 70
RSI_OVERBOUGHT = 75


def risk_levels(entry_price: float) -> tuple[float, float]:
    """Stop-loss and take-profit prices for a long entry at ``entry_price``."""
    return entry_price * (1 - STOP_LOSS_PCT), entry_price * (1 + TAKE_PROFIT_PCT)


def generate_signal(symbol: str, asset_class: AssetClass, bars: list[Bar]) -> Signal:
    if len(bars) < MIN_BARS:
        raise ValueError(f"At least {MIN_BARS} bars are required for the default strategy.")

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

    if bullish_cross and latest.rsi_14 < RSI_BUY_MAX:
        confidence = 0.65
        reason_parts = ["sma20_crossed_above_sma50", f"rsi={latest.rsi_14:.1f}"]
        if bullish_patterns:
            confidence += 0.1
            reason_parts.append(f"bullish_candle_pattern={bullish_patterns[0]}")
        stop_loss, take_profit = risk_levels(latest.close)
        return Signal(
            symbol=symbol,
            asset_class=asset_class,
            action=SignalAction.BUY,
            confidence=min(confidence, 0.95),
            reason=", ".join(reason_parts),
            latest_close=latest.close,
            timestamp=latest.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    if bearish_cross or latest.rsi_14 > RSI_OVERBOUGHT:
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
