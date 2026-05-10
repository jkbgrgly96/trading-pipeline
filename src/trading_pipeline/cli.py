from __future__ import annotations

import argparse
import sys

from trading_pipeline.brokers.alpaca import AlpacaBroker
from trading_pipeline.config import Settings, load_settings
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications.console import ConsoleNotifier
from trading_pipeline.notifications.telegram import TelegramNotifier
from trading_pipeline.pipeline import run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the trading alert pipeline.")
    parser.add_argument("--symbol", help="Ticker or crypto pair, e.g. AAPL or BTC/USD.")
    parser.add_argument(
        "--asset-class",
        choices=[item.value for item in AssetClass],
        help="Asset class to request from Alpaca.",
    )
    parser.add_argument("--timeframe", help="Supported: 1Min, 1Hour, 1Day.")
    parser.add_argument("--limit", type=int, help="Number of bars to request.")
    parser.add_argument("--notional", type=float, help="Paper order notional size.")
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send Telegram alert when TELEGRAM_* env vars are configured.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    symbol = args.symbol or settings.default_symbol
    asset_class = AssetClass(args.asset_class or settings.default_asset_class)
    timeframe = args.timeframe or settings.default_timeframe
    limit = args.limit or settings.default_bar_limit
    notional = args.notional or settings.default_notional

    broker = AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
    )
    notifier = _build_notifier(args.telegram, settings)

    result = run_once(
        broker=broker,
        notifier=notifier,
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        limit=limit,
        orders_enabled=settings.orders_enabled,
        notional=notional,
    )

    if result.order_id:
        print(f"paper_order_id={result.order_id}")
    else:
        print("order_submission=disabled_or_not_actionable")

    return 0


def _build_notifier(use_telegram: bool, settings: Settings):
    if (
        use_telegram
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    ):
        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return ConsoleNotifier()


if __name__ == "__main__":
    sys.exit(main())
