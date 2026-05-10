from __future__ import annotations

from dataclasses import dataclass

from trading_pipeline.brokers.base import Broker
from trading_pipeline.domain import AssetClass, Signal
from trading_pipeline.notifications.base import Notifier
from trading_pipeline.strategies.sma_rsi import generate_signal


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
) -> PipelineResult:
    bars = broker.get_bars(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        limit=limit,
    )
    signal = generate_signal(symbol=symbol, asset_class=asset_class, bars=bars)
    notifier.send(signal)

    order_id = None
    if orders_enabled and signal.is_actionable:
        order_id = broker.submit_signal_order(signal, notional=notional)

    return PipelineResult(signal=signal, order_id=order_id)
