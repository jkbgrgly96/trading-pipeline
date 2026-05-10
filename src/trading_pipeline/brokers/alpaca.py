from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trading_pipeline.brokers.base import Broker
from trading_pipeline.domain import AssetClass, Bar, Signal, SignalAction


class AlpacaBroker(Broker):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")

        try:
            from alpaca.data.historical import CryptoHistoricalDataClient
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:
            raise RuntimeError(
                "alpaca-py is not installed. Run: pip install -e ."
            ) from exc

        self._stock_data_client = StockHistoricalDataClient(api_key, secret_key)
        self._crypto_data_client = CryptoHistoricalDataClient(api_key, secret_key)
        self._trading_client = TradingClient(api_key, secret_key, paper=paper)

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        limit: int,
    ) -> list[Bar]:
        end = datetime.now(UTC)
        start = end - _lookback_for(timeframe, limit)

        if asset_class == AssetClass.CRYPTO:
            return self._get_crypto_bars(symbol, timeframe, start, end, limit)
        return self._get_stock_bars(symbol, timeframe, start, end, limit)

    def _get_stock_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[Bar]:
        from alpaca.data.requests import StockBarsRequest

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_to_alpaca_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
        )
        response = self._stock_data_client.get_stock_bars(request)
        return _extract_bars(response, symbol)

    def _get_crypto_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[Bar]:
        from alpaca.data.requests import CryptoBarsRequest

        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_to_alpaca_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
        )
        response = self._crypto_data_client.get_crypto_bars(request)
        return _extract_bars(response, symbol)

    def submit_signal_order(self, signal: Signal, notional: float) -> str:
        if signal.action == SignalAction.HOLD:
            raise ValueError("HOLD signals cannot be submitted as orders.")

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
        time_in_force = (
            TimeInForce.GTC
            if signal.asset_class == AssetClass.CRYPTO
            else TimeInForce.DAY
        )
        request = MarketOrderRequest(
            symbol=signal.symbol,
            notional=notional,
            side=side,
            time_in_force=time_in_force,
        )
        order = self._trading_client.submit_order(order_data=request)
        return str(order.id)


def _to_alpaca_timeframe(value: str) -> Any:
    from alpaca.data.timeframe import TimeFrame

    normalized = value.strip().lower()
    mapping = {
        "1min": TimeFrame.Minute,
        "1minute": TimeFrame.Minute,
        "1hour": TimeFrame.Hour,
        "1h": TimeFrame.Hour,
        "1day": TimeFrame.Day,
        "1d": TimeFrame.Day,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe: {value}") from exc


def _lookback_for(timeframe: str, limit: int) -> timedelta:
    normalized = timeframe.strip().lower()
    if normalized in {"1min", "1minute"}:
        return timedelta(minutes=max(limit * 3, 300))
    if normalized in {"1hour", "1h"}:
        return timedelta(hours=max(limit * 3, 240))
    return timedelta(days=max(limit * 3, 365))


def _extract_bars(response: Any, symbol: str) -> list[Bar]:
    raw_bars = response.data.get(symbol, [])
    bars = [
        Bar(
            timestamp=bar.timestamp,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )
        for bar in raw_bars
    ]
    return sorted(bars, key=lambda bar: bar.timestamp)
