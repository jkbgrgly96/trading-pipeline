from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from trading_pipeline.config import Settings, load_settings
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications.base import MultiNotifier, Notifier
from trading_pipeline.notifications.console import ConsoleNotifier
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import available_strategies, get_strategy
from trading_pipeline.timeframes import parse_timeframe

if TYPE_CHECKING:
    from trading_pipeline.watch import WatchItem

logger = logging.getLogger("trading_pipeline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Alert-first trading pipeline. By default runs one alert pass. "
            "Use --watch to keep running over a watchlist, or --backtest."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backtest",
        action="store_true",
        help="Run a historical backtest of --symbol, or of every watchlist entry when "
        "--symbol is not given.",
    )
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
    parser.add_argument(
        "--notional", type=float, help="USD per paper order, and per backtest entry."
    )
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
    backtest.add_argument(
        "--start",
        help="First date to trade, e.g. 2021-01-01 (earlier bars are fetched for indicator "
        "warm-up). Without it, the latest --limit bars are used.",
    )
    backtest.add_argument(
        "--end", help="Last date to trade, inclusive, e.g. 2025-12-31 (default: now)."
    )
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
        help="Size entries as this fraction of current equity (0 < size <= 1, compounding) "
        "instead of --notional USD like paper orders.",
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
        help="Smallest tradable fraction of a share or coin in backtests.",
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
        if args.position_size is not None and not 0 < args.position_size <= 1:
            raise ValueError("--position-size must be in (0, 1].")
        if limit <= 0 or notional <= 0:
            raise ValueError("--limit and --notional must be positive.")
        start = _parse_date(args.start)
        end = _parse_date(args.end, inclusive_end=True)
        if (start or end) and not args.backtest:
            raise ValueError("--start/--end only apply to --backtest.")
        if start and end and start >= end:
            raise ValueError("--start must be before --end.")
    except ValueError as exc:
        parser.error(str(exc))

    from trading_pipeline.brokers.alpaca import AlpacaBroker

    broker = AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
        stock_feed=settings.alpaca_stock_feed,
        adjustment=settings.alpaca_adjustment,
    )
    storage = Storage(settings.database_path)
    storage.initialize()

    def watch_items(use_watchlist: bool) -> list[WatchItem]:
        from trading_pipeline.watch import WatchItem, load_watchlist

        watchlist_path = Path(args.watchlist or settings.watchlist_path)
        if use_watchlist and watchlist_path.exists():
            return load_watchlist(watchlist_path, limit, notional, strategy_name)
        if use_watchlist and args.watchlist:
            parser.error(f"Watchlist not found: {watchlist_path}")
        return [WatchItem(symbol, asset_class, timeframe, limit, notional, strategy_name)]

    if args.backtest:
        items = watch_items(use_watchlist=args.symbol is None)
        if args.limit is not None:
            # History length is a backtest setting: --limit applies to every entry.
            items = [replace(item, limit=args.limit) for item in items]
        try:
            from trading_pipeline.watch import validate_watchlist

            validate_watchlist(items, orders_enabled=False)
        except ValueError as exc:
            parser.error(str(exc))
        return _run_backtests(args, broker, storage, items, start, end)

    notifier = build_notifier(args.telegram, settings)
    _log_mode(settings)
    notify_hold = args.notify_hold or settings.notify_hold

    if args.watch or args.once:
        from trading_pipeline.watch import Watcher

        watcher = Watcher(
            broker=broker,
            storage=storage,
            notifier=notifier,
            items=watch_items(use_watchlist=True),
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


def _parse_date(value: str | None, inclusive_end: bool = False) -> datetime | None:
    """ISO date or datetime, UTC unless it carries an offset.

    A date-only end (``2025-12-31``) includes that whole day.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; use YYYY-MM-DD.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if inclusive_end and len(value.strip()) == 10:
        parsed += timedelta(days=1)
    return parsed


def _run_backtests(args, broker, storage, items: list[WatchItem], start, end) -> int:
    rows = []
    for item in items:
        try:
            run_id, config, summary = _backtest_item(args, broker, storage, item, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
            logger.error("Backtest failed for %s: %s", item.label, exc)
            logger.debug("Backtest traceback", exc_info=True)
            rows.append((item, None, None, None))
            continue
        rows.append((item, run_id, config, summary))

    if len(rows) == 1 and rows[0][1] is not None:
        _, run_id, config, summary = rows[0]
        for key, value in summary.items():
            print(f"{key}={value}")
        print(
            f"strategy={config.strategy} sizing={config.sizing} notional={config.notional:g} "
            f"commission={config.commission} spread={config.spread}"
        )
        print(f"backtest_run_id={run_id}")
    else:
        _print_backtest_table(rows)
    return 1 if any(run_id is None for _, run_id, _, _ in rows) else 0


def _backtest_item(args, broker, storage, item: WatchItem, start, end):
    from trading_pipeline.backtest.runner import (
        BacktestConfig,
        default_costs,
        fetch_backtest_bars,
        run_strategy_backtest,
        summarize_stats,
    )

    default_commission, default_spread = default_costs(item.asset_class)
    config = BacktestConfig(
        symbol=item.symbol,
        asset_class=item.asset_class,
        strategy=item.strategy,
        cash=args.cash,
        commission=args.commission if args.commission is not None else default_commission,
        spread=args.spread if args.spread is not None else default_spread,
        notional=item.notional,
        position_size=args.position_size,
        trade_on_close=args.trade_on_close,
        fractional_unit=args.fractional_unit,
        start=start,
    )
    config.validate()
    bars = fetch_backtest_bars(
        broker,
        item.symbol,
        item.asset_class,
        item.timeframe,
        min_bars=get_strategy(item.strategy).min_bars,
        limit=item.limit,
        start=start,
        end=end,
    )
    result = run_strategy_backtest(bars=bars, config=config)
    storage.save_market_bars(item.symbol, item.asset_class, item.timeframe, bars, source="alpaca")
    storage.save_feature_snapshots(item.symbol, item.asset_class, item.timeframe, result.feature_frame)
    run_id = storage.save_backtest(
        symbol=item.symbol,
        asset_class=item.asset_class,
        timeframe=item.timeframe,
        bar_limit=len(bars),
        config=config,
        result=result,
    )
    return run_id, config, summarize_stats(result.stats)


def _print_backtest_table(rows) -> None:
    header = (
        f"{'entry':<28} {'strategy':<10} {'trades':>6} {'win %':>6} {'P/L $':>10} "
        f"{'avg trade %':>11} {'max DD %':>9} {'B&H %':>8} {'run':>5}"
    )
    print(header)
    print("-" * len(header))
    total_trades, total_pnl = 0, 0.0
    for item, run_id, _config, summary in rows:
        if run_id is None:
            print(f"{item.label:<28} {item.strategy:<10} {'error (see log)':>6}")
            continue
        trades = int(summary.get("# Trades", 0) or 0)
        pnl = float(summary.get("P/L [$]", 0.0))
        total_trades += trades
        total_pnl += pnl
        print(
            f"{item.label:<28} {item.strategy:<10} {trades:>6} "
            f"{_fmt(summary.get('Win Rate [%]'), 1):>6} {pnl:>10.2f} "
            f"{_fmt(summary.get('Avg. Trade [%]'), 2):>11} "
            f"{_fmt(summary.get('Max. Drawdown [%]'), 2):>9} "
            f"{_fmt(summary.get('Buy & Hold Return [%]'), 1):>8} {run_id:>5}"
        )
    print("-" * len(header))
    print(f"{'total':<28} {'':<10} {total_trades:>6} {'':>6} {total_pnl:>10.2f}")


def _fmt(value, decimals: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if number != number else f"{number:.{decimals}f}"


if __name__ == "__main__":
    sys.exit(main())
