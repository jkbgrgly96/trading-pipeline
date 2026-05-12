from __future__ import annotations

from dataclasses import dataclass

from trading_pipeline.brokers.base import Broker
from trading_pipeline.domain import AssetClass, Signal
from trading_pipeline.features import compute_feature_frame, latest_snapshot, snapshot_from_frame
from trading_pipeline.notifications.base import Notifier
from trading_pipeline.storage import Storage
from trading_pipeline.strategies.sma_rsi import generate_signal_from_features


@dataclass(frozen=True)
class PipelineResult:
    signal: Signal
    order_id: str | None


def run_once(
    broker: Broker,
    notifier: Notifier,
    symbol: str,
    asset_class: AssetClass,
    timeframe: str,
    limit: int,
    orders_enabled: bool,
    notional: float,
    storage: Storage | None = None,
    source: str = "alpaca",
) -> PipelineResult:
    bars = broker.get_bars(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        limit=limit,
    )
    feature_frame = compute_feature_frame(bars)
    latest = latest_snapshot(feature_frame)
    previous = snapshot_from_frame(feature_frame, -2)
    signal = generate_signal_from_features(
        symbol=symbol,
        asset_class=asset_class,
        latest=latest,
        previous=previous,
    )

    signal_id = None
    if storage:
        storage.save_market_bars(symbol, asset_class, timeframe, bars, source=source)
        storage.save_feature_snapshots(symbol, asset_class, timeframe, feature_frame)
        signal_id = storage.save_signal(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            signal=signal,
            snapshot=latest,
            orders_enabled=orders_enabled,
        )

    notifier.send(signal)

    order_id = None
    if orders_enabled and signal.is_actionable:
        order_id = broker.submit_signal_order(signal, notional=notional)
        if storage and signal_id:
            storage.update_signal_order_id(signal_id, order_id)

    return PipelineResult(signal=signal, order_id=order_id)
