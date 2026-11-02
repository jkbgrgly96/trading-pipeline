from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trading_pipeline.brokers.base import Broker, BrokerOrder, BrokerPosition
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.timeframes import parse_timeframe

# The free market data plan serves SIP (full market) bars only once they are
# older than 15 minutes; a minute of margin avoids edge-of-window refusals.
SIP_DELAY = timedelta(minutes=16)


class AlpacaBroker(Broker):
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        stock_feed: str = "sip",
        adjustment: str = "split",
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")

        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._paper = bool(paper)
        self._stock_data_client = StockHistoricalDataClient(api_key, secret_key)
        self._crypto_data_client = CryptoHistoricalDataClient(api_key, secret_key)
        # Defence in depth: a live (real money) trading client is never
        # created. Market data works with either key type.
        self._trading_client = (
            TradingClient(api_key, secret_key, paper=True) if self._paper else None
        )
        self._stock_feed = _to_data_feed(stock_feed)
        self._stock_delay = SIP_DELAY if self._stock_feed.value == "sip" else timedelta(0)
        # Raw stock bars show a split as a crash (or a jump), which fires stops
        # and fake crossovers in backtests and in live indicators alike.
        self._adjustment = _to_adjustment(adjustment)

    @property
    def is_paper(self) -> bool:
        return self._paper

    def data_delay(self, asset_class: AssetClass) -> timedelta:
        return self._stock_delay if asset_class == AssetClass.STOCK else timedelta(0)

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        limit: int,
    ) -> list[Bar]:
        parsed = parse_timeframe(timeframe)
        end = datetime.now(UTC)
        start = end - parsed.lookback(asset_class, limit)
        # Newest first, so ``limit`` keeps the latest bars of the window.
        return self._fetch_bars(symbol, asset_class, timeframe, start, end, limit, newest_first=True)

    def get_bars_between(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        # No limit: the SDK follows the page tokens until ``end``.
        return self._fetch_bars(symbol, asset_class, timeframe, start, end, None, newest_first=False)

    def _fetch_bars(
        self,
        symbol: str,
        asset_class: AssetClass,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int | None,
        newest_first: bool,
    ) -> list[Bar]:
        from alpaca.common.enums import Sort

        end = min(end, datetime.now(UTC) - self.data_delay(asset_class))
        common: dict[str, Any] = {
            "symbol_or_symbols": symbol,
            "timeframe": parse_timeframe(timeframe).to_alpaca(),
            "start": start,
            "end": end,
            "limit": limit,
            "sort": Sort.DESC if newest_first else Sort.ASC,
        }
        if asset_class == AssetClass.CRYPTO:
            from alpaca.data.requests import CryptoBarsRequest

            response = self._crypto_data_client.get_crypto_bars(CryptoBarsRequest(**common))
        else:
            from alpaca.data.requests import StockBarsRequest

            request = StockBarsRequest(**common, feed=self._stock_feed, adjustment=self._adjustment)
            response = self._stock_data_client.get_stock_bars(request)
        return _extract_bars(response, symbol)

    # --- trading (paper only) -------------------------------------------

    @property
    def _trading(self) -> Any:
        self.ensure_paper()
        return self._trading_client

    def get_position(self, symbol: str, asset_class: AssetClass) -> BrokerPosition | None:
        from alpaca.common.exceptions import APIError

        try:
            position = self._trading.get_open_position(_position_symbol(symbol))
        except APIError as exc:
            if _status_code(exc) == 404:
                return None
            raise
        quantity = float(position.qty)
        if quantity <= 0:
            # A short (or zero) position is not something this long-only
            # pipeline manages; treat it as flat for entry/exit purposes.
            return None
        return BrokerPosition(
            symbol=symbol,
            quantity=quantity,
            avg_entry_price=float(position.avg_entry_price),
        )

    def submit_market_buy(self, symbol: str, asset_class: AssetClass, notional: float) -> str:
        self.ensure_paper()
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        # Notional (fractional) stock orders must be DAY; crypto uses GTC.
        time_in_force = TimeInForce.GTC if asset_class == AssetClass.CRYPTO else TimeInForce.DAY
        request = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=OrderSide.BUY,
            time_in_force=time_in_force,
        )
        order = self._trading.submit_order(order_data=request)
        return str(order.id)

    def close_position(self, symbol: str, asset_class: AssetClass) -> str | None:
        self.ensure_paper()
        from alpaca.common.exceptions import APIError

        try:
            order = self._trading.close_position(_position_symbol(symbol))
        except APIError as exc:
            if _status_code(exc) == 404:
                return None
            raise
        return str(order.id)

    def cancel_order(self, order_id: str) -> None:
        self.ensure_paper()
        from alpaca.common.exceptions import APIError

        try:
            self._trading.cancel_order_by_id(order_id)
        except APIError as exc:
            # 404: unknown order; 422: already filled/canceled. Nothing to do.
            if _status_code(exc) in {404, 422}:
                return
            raise

    def get_order(self, order_id: str) -> BrokerOrder:
        order = self._trading.get_order_by_id(order_id)
        status = order.status
        # Alpaca reports filled_avg_price as None or "0" until something fills.
        price = float(order.filled_avg_price or 0) or None
        return BrokerOrder(
            id=str(order.id),
            status=str(getattr(status, "value", status)).lower(),
            filled_qty=float(order.filled_qty or 0),
            filled_avg_price=price,
            filled_at=order.filled_at,
        )


def _position_symbol(symbol: str) -> str:
    # Alpaca positions use "BTCUSD" for the "BTC/USD" pair.
    return symbol.replace("/", "")


def _status_code(exc: Exception) -> int | None:
    try:
        return int(exc.status_code)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def _to_data_feed(value: str) -> Any:
    from alpaca.data.enums import DataFeed

    normalized = value.strip().lower()
    mapping = {
        "iex": DataFeed.IEX,
        "sip": DataFeed.SIP,
        "delayed_sip": DataFeed.DELAYED_SIP,
        "otc": DataFeed.OTC,
        "boats": DataFeed.BOATS,
        "overnight": DataFeed.OVERNIGHT,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Alpaca stock feed: {value}") from exc


def _to_adjustment(value: str) -> Any:
    from alpaca.data.enums import Adjustment

    try:
        return Adjustment(value.strip().lower())
    except ValueError as exc:
        options = ", ".join(item.value for item in Adjustment)
        raise ValueError(f"Unsupported Alpaca adjustment {value!r}; use one of {options}.") from exc


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
