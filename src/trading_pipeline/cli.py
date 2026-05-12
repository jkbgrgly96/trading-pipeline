from __future__ import annotations

import argparse
import sys

from trading_pipeline.backtest.runner import (
    BacktestConfig,
    run_strategy_backtest,
    summarize_stats,
)
from trading_pipeline.brokers.alpaca import AlpacaBroker
from trading_pipeline.config import Settings, load_settings
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications.console import ConsoleNotifier
from trading_pipeline.notifications.telegram import TelegramNotifier
from trading_pipeline.pipeline import run_once
from trading_pipeline.storage import Storage


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
        "--backtest",
        action="store_true",
        help="Run a historical backtest instead of a live alert pass.",
    )
    parser.add_argument("--cash", type=float, default=10_000.0, help="Backtest cash.")
    parser.add_argument(
        "--commission",
        type=float,
        default=0.0,
        help="Backtest commission rate, e.g. 0.001 for 0.1%%.",
    )
    parser.add_argument(
        "--spread",
        type=float,
        default=0.0,
        help="Backtest spread rate, e.g. 0.0002.",
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=0.95,
        help="Fraction of available equity to use on BUY signals.",
    )
    parser.add_argument(
        "--trade-on-close",
        action="store_true",
        help="Execute backtest trades on bar close instead of next bar open.",
    )
    parser.add_argument(
        "--fractional-unit",
        type=float,
        default=0.00000001,
        help="Backtesting.py fractional unit for crypto backtests.",
    )
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
        stock_feed=settings.alpaca_stock_feed,
    )
    storage = Storage(settings.database_path)
    storage.initialize()

    if args.backtest:
        backtest_config = BacktestConfig(
            symbol=symbol,
            asset_class=asset_class,
            cash=args.cash,
            commission=args.commission,
            spread=args.spread,
            position_size=args.position_size,
            trade_on_close=args.trade_on_close,
            fractional_unit=args.fractional_unit,
        )
        bars = broker.get_bars(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            limit=limit,
        )
        result = run_strategy_backtest(
            bars=bars,
            config=backtest_config,
        )
        storage.save_market_bars(symbol, asset_class, timeframe, bars, source="alpaca")
        storage.save_feature_snapshots(
            symbol,
            asset_class,
            timeframe,
            result.feature_frame,
        )
        backtest_run_id = storage.save_backtest(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            bar_limit=limit,
            config=backtest_config,
            result=result,
        )
        _print_backtest_summary(summarize_stats(result.stats))
        print(f"backtest_run_id={backtest_run_id}")
        return 0

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
        storage=storage,
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


def _print_backtest_summary(summary: dict[str, object]) -> None:
    for key, value in summary.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    sys.exit(main())
