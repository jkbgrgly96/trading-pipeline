from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path

from trading_pipeline.config import Settings, load_settings
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications.base import MultiNotifier, Notifier
from trading_pipeline.notifications.console import ConsoleNotifier
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import available_strategies, get_strategy
from trading_pipeline.timeframes import parse_timeframe

logger = logging.getLogger("trading_pipeline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Alert-first trading pipeline. By default runs one alert pass. "
            "Use --watch to keep running over a watchlist, or --backtest."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backtest", action="store_true", help="Run a historical backtest.")
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously over the watchlist and alert on every closed bar.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Process every watchlist entry once and exit (for Task Scheduler / cron).",
    )

    parser.add_argument("--symbol", help="Ticker or crypto pair, e.g. AAPL or BTC/USD.")
    parser.add_argument(
        "--asset-class",
        choices=[item.value for item in AssetClass],
        help="Asset class to request from Alpaca.",
    )
    parser.add_argument("--timeframe", help="e.g. 1Min, 5Min, 15Min, 1Hour, 4Hour, 1Day.")
    parser.add_argument(
        "--strategy",
        choices=available_strategies(),
        help="Strategy for a single symbol or backtest, and the watchlist default "
        "(default: DEFAULT_STRATEGY or sma_rsi).",
    )
    parser.add_argument("--limit", type=int, help="Number of bars to request.")
    parser.add_argument("--notional", type=float, help="Paper order size in USD.")
    parser.add_argument(
        "--watchlist",
        help="TOML watchlist for --watch/--once (default: WATCHLIST_PATH or watchlist.toml). "
        "Falls back to --symbol/--timeframe when the file does not exist.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="Seconds between watch cycles (default: shortest timeframe, 60s-900s).",
    )
    parser.add_argument(
        "--notify-hold",
        action="store_true",
        help="Also send a notification when there is nothing to do.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send alerts to Telegram (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID).",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ...")

    backtest = parser.add_argument_group("backtest options")
    backtest.add_argument("--cash", type=float, default=10_000.0, help="Backtest cash.")
    backtest.add_argument(
        "--commission",
        type=float,
        help="Commission rate, e.g. 0.001 for 0.1%%. Default: 0 for stocks, 0.0025 for crypto.",
    )
    backtest.add_argument(
        "--spread", type=float, help="Spread rate. Default: 0.0005 (5 bps)."
    )
    backtest.add_argument(
        "--position-size",
        type=float,
        default=0.95,
        help="Fraction of equity per entry (0 < size < 1).",
    )
    backtest.add_argument(
        "--trade-on-close",
        action="store_true",
        help="Fill at the signal bar's close instead of the next bar's open.",
    )
    backtest.add_argument(
        "--fractional-unit",
        type=float,
        default=0.00000001,
        help="Backtesting.py fractional unit for crypto backtests.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    symbol = args.symbol or settings.default_symbol
    asset_class = AssetClass(args.asset_class or settings.default_asset_class)
    timeframe = args.timeframe or settings.default_timeframe
    strategy_name = args.strategy or settings.default_strategy
    limit = args.limit if args.limit is not None else settings.default_bar_limit
    notional = args.notional if args.notional is not None else settings.default_notional

    try:
        parse_timeframe(timeframe)
        strategy = get_strategy(strategy_name)
        if not 0 < args.position_size <= 1:
            raise ValueError("--position-size must be in (0, 1].")
        if limit <= 0 or notional <= 0:
            raise ValueError("--limit and --notional must be positive.")
    except ValueError as exc:
        parser.error(str(exc))

    from trading_pipeline.brokers.alpaca import AlpacaBroker

    broker = AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
        stock_feed=settings.alpaca_stock_feed,
    )
    storage = Storage(settings.database_path)
    storage.initialize()

    if args.backtest:
        return _run_backtest(args, broker, storage, symbol, asset_class, timeframe, limit, strategy_name)

    notifier = build_notifier(args.telegram, settings)
    _log_mode(settings)
    notify_hold = args.notify_hold or settings.notify_hold

    if args.watch or args.once:
        from trading_pipeline.watch import Watcher, WatchItem, load_watchlist

        watchlist_path = Path(args.watchlist or settings.watchlist_path)
        if watchlist_path.exists():
            items = load_watchlist(watchlist_path, limit, notional, strategy_name)
        else:
            if args.watchlist:
                parser.error(f"Watchlist not found: {watchlist_path}")
            items = [WatchItem(symbol, asset_class, timeframe, limit, notional, strategy_name)]
        watcher = Watcher(
            broker=broker,
            storage=storage,
            notifier=notifier,
            items=items,
            orders_enabled=settings.orders_enabled,
            notify_hold=notify_hold,
        )
        if args.once:
            outcomes = watcher.run_cycle()
            for label, outcome in outcomes.items():
                print(f"{label}: {outcome}")
            return 1 if "error" in outcomes.values() else 0
        interval = timedelta(seconds=args.interval) if args.interval else None
        watcher.run_forever(interval=interval)
        return 0

    from trading_pipeline.pipeline import flush_outbox, run_once

    flush_outbox(storage, notifier)
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
        notify_hold=notify_hold,
        strategy=strategy,
    )
    if result.status == "already_processed":
        print(f"bar {result.bar_timestamp.isoformat()} already processed; nothing to do")
    elif result.decision is not None:
        print(f"decision={result.decision.action.value} reason={result.decision.reason}")
        if result.order_id:
            print(f"paper_order_id={result.order_id}")
    return 0


def build_notifier(use_telegram: bool, settings: Settings) -> Notifier:
    console = ConsoleNotifier()
    if not use_telegram:
        return console
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise SystemExit("--telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    from trading_pipeline.notifications.telegram import TelegramNotifier

    telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return MultiNotifier([telegram], optional=[console])


def _log_mode(settings: Settings) -> None:
    if settings.orders_enabled:
        logger.warning("PAPER ORDERS ENABLED: decisions will place orders on the Alpaca paper account.")
    else:
        logger.info("Alerts only: no orders will be placed.")
    if not settings.alpaca_paper:
        logger.warning("ALPACA_PAPER=false: trading endpoints are disabled; only market data is used.")


def _run_backtest(args, broker, storage, symbol, asset_class, timeframe, limit, strategy_name) -> int:
    from trading_pipeline.backtest.runner import (
        BacktestConfig,
        default_costs,
        run_strategy_backtest,
        summarize_stats,
    )

    default_commission, default_spread = default_costs(asset_class)
    config = BacktestConfig(
        symbol=symbol,
        asset_class=asset_class,
        strategy=strategy_name,
        cash=args.cash,
        commission=args.commission if args.commission is not None else default_commission,
        spread=args.spread if args.spread is not None else default_spread,
        position_size=args.position_size,
        trade_on_close=args.trade_on_close,
        fractional_unit=args.fractional_unit,
    )
    from datetime import UTC, datetime

    from trading_pipeline.timeframes import closed_bars

    bars = broker.get_bars(symbol=symbol, asset_class=asset_class, timeframe=timeframe, limit=limit)
    bars = closed_bars(bars, parse_timeframe(timeframe), datetime.now(UTC))
    result = run_strategy_backtest(bars=bars, config=config)
    storage.save_market_bars(symbol, asset_class, timeframe, bars, source="alpaca")
    storage.save_feature_snapshots(symbol, asset_class, timeframe, result.feature_frame)
    run_id = storage.save_backtest(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        bar_limit=limit,
        config=config,
        result=result,
    )
    for key, value in summarize_stats(result.stats).items():
        print(f"{key}={value}")
    print(f"strategy={config.strategy} commission={config.commission} spread={config.spread}")
    print(f"backtest_run_id={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
