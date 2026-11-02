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
from trading_pipeline.sizing import Sizing
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import (
    available_strategies,
    get_strategy,
    parse_param_assignments,
)
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
    mode.add_argument(
        "--sweep",
        action="store_true",
        help="Tune --strategy over the watchlist: every --grid combination on --start..--split, "
        "then the best few (plus defaults and sma_rsi) on --split..--end.",
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
        help="Strategy for a single symbol, the watchlist default, and in a backtest the "
        "strategy run on every watchlist entry (default: DEFAULT_STRATEGY or sma_rsi).",
    )
    parser.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="Strategy parameter override, repeatable (e.g. --param fast=10). Applies to "
        "entries that run the --strategy strategy.",
    )
    parser.add_argument("--limit", type=int, help="Number of bars to request.")
    parser.add_argument(
        "--notional",
        type=float,
        help="USD per entry with --sizing notional (and the fallback without a stop).",
    )
    parser.add_argument(
        "--sizing",
        choices=["risk", "notional"],
        help="risk: buy the amount that loses --risk-per-trade if the stop is hit, capped at "
        "--max-notional; notional: --notional per entry (default: SIZING or risk). Used by "
        "paper orders, alerts and backtests alike.",
    )
    parser.add_argument("--risk-per-trade", type=float, help="USD at risk per entry (default: 10).")
    parser.add_argument("--max-notional", type=float, help="Largest entry in USD (default: 1000).")
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
        "--max-open-positions",
        type=int,
        help="Skip BUY signals while this many positions are open across all symbols "
        "(default: MAX_OPEN_POSITIONS; 0 = no limit). Exits are never blocked.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send alerts to Telegram (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID).",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ...")

    sweep = parser.add_argument_group("sweep options")
    sweep.add_argument(
        "--grid",
        action="append",
        metavar="KEY=V1,V2",
        help="Values to try for a strategy parameter, repeatable (default: a built-in grid).",
    )
    sweep.add_argument(
        "--split", help="First out-of-sample date (default: 2024-01-01); tuning uses --start..--split."
    )
    sweep.add_argument("--top", type=int, default=5, help="Configurations re-run out of sample.")
    sweep.add_argument(
        "--min-trades",
        type=int,
        default=30,
        help="Fewer in-sample trades (all entries together) cannot be a finalist.",
    )

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
    sizing_values = {
        "mode": args.sizing or settings.sizing,
        "notional": notional,
        "risk_per_trade": args.risk_per_trade if args.risk_per_trade is not None else settings.risk_per_trade,
        "max_notional": args.max_notional if args.max_notional is not None else settings.max_notional,
    }

    try:
        parse_timeframe(timeframe)
        cli_params = parse_param_assignments(args.param)
        sizing = Sizing(**sizing_values)
        strategy = get_strategy(strategy_name, cli_params)
        if args.position_size is not None and not 0 < args.position_size <= 1:
            raise ValueError("--position-size must be in (0, 1].")
        if limit <= 0 or notional <= 0:
            raise ValueError("--limit and --notional must be positive.")
        max_open_positions = (
            args.max_open_positions
            if args.max_open_positions is not None
            else settings.max_open_positions
        )
        if max_open_positions is not None and max_open_positions < 0:
            raise ValueError("--max-open-positions must be >= 0.")
        max_open_positions = max_open_positions or None
        start = _parse_date(args.start)
        end = _parse_date(args.end, inclusive_end=True)
        if (start or end) and not (args.backtest or args.sweep):
            raise ValueError("--start/--end only apply to --backtest and --sweep.")
        if args.sweep and not args.strategy:
            raise ValueError("--sweep needs --strategy.")
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
        from trading_pipeline.watch import WatchItem, freeze_params, load_watchlist

        watchlist_path = Path(args.watchlist or settings.watchlist_path)
        if use_watchlist and watchlist_path.exists():
            try:
                items = load_watchlist(watchlist_path, limit, notional, strategy_name, sizing)
                return [
                    replace(item, params=freeze_params({**item.strategy_params, **cli_params}))
                    if cli_params and item.strategy == strategy_name
                    else item
                    for item in items
                ]
            except ValueError as exc:
                parser.error(f"{watchlist_path}: {exc}")
        if use_watchlist and args.watchlist:
            parser.error(f"Watchlist not found: {watchlist_path}")
        return [
            WatchItem(
                symbol, asset_class, timeframe, limit, notional, strategy_name,
                freeze_params(cli_params), sizing.mode.value, sizing.risk_per_trade,
                sizing.max_notional,
            )
        ]

    if args.backtest or args.sweep:
        from trading_pipeline.watch import freeze_params, validate_watchlist

        items = watch_items(use_watchlist=args.symbol is None)
        if args.strategy:
            # Backtesting a strategy means running it on every entry.
            items = [
                item
                if item.strategy == args.strategy
                else replace(item, strategy=args.strategy, params=freeze_params(cli_params))
                for item in items
            ]
        if args.limit is not None:
            # History length is a backtest setting: --limit applies to every entry.
            items = [replace(item, limit=args.limit) for item in items]
        try:
            validate_watchlist(items, orders_enabled=False)
        except ValueError as exc:
            parser.error(str(exc))
        if args.sweep:
            try:
                return _run_sweep(args, broker, items, cli_params, start, end)
            except ValueError as exc:
                parser.error(str(exc))
        return _run_backtests(args, broker, storage, items, start, end)

    notifier = build_notifier(args.telegram, settings)
    _log_mode(settings, max_open_positions)
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
            max_open_positions=max_open_positions,
        )
        if args.once:
            outcomes = watcher.run_cycle()
            for label, outcome in outcomes.items():
                print(f"{label}: {outcome}")
            return 1 if "error" in outcomes.values() else 0
        interval = timedelta(seconds=args.interval) if args.interval else None
        watcher.run_forever(interval=interval)
        return 0

    from trading_pipeline.pipeline import flush_outbox, run_once, sync_order_fills

    flush_outbox(storage, notifier)
    if settings.orders_enabled:
        sync_order_fills(storage, broker)
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
        max_open_positions=max_open_positions,
        sizing=sizing,
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


def _log_mode(settings: Settings, max_open_positions: int | None) -> None:
    if max_open_positions:
        logger.info("At most %d open positions: further BUY signals are skipped.", max_open_positions)
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
        risk = (
            f" risk_per_trade={config.risk_per_trade:g} max_notional={config.max_notional:g}"
            if config.sizing == "risk"
            else ""
        )
        print(
            f"strategy={config.strategy} sizing={config.sizing} notional={config.notional:g}{risk} "
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
        strategy_params=item.strategy_params,
        cash=args.cash,
        commission=args.commission if args.commission is not None else default_commission,
        spread=args.spread if args.spread is not None else default_spread,
        notional=item.notional,
        risk_per_trade=item.risk_per_trade if item.sizing.mode.value == "risk" else None,
        max_notional=item.max_notional,
        position_size=args.position_size,
        trade_on_close=args.trade_on_close,
        fractional_unit=args.fractional_unit,
        start=start,
    )
    config.validate()
    strategy = item.build_strategy()

    def fetch(symbol: str) -> list:
        return fetch_backtest_bars(
            broker,
            symbol,
            item.asset_class,
            item.timeframe,
            min_bars=strategy.min_bars,
            limit=item.limit,
            start=start,
            end=end,
        )

    bars = fetch(item.symbol)
    benchmark = strategy.benchmark_for(item.symbol, item.asset_class)
    result = run_strategy_backtest(
        bars=bars, config=config, benchmark_bars=fetch(benchmark) if benchmark else None
    )
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


def _run_sweep(args, broker, items: list[WatchItem], base_params: dict, start, end) -> int:
    import csv
    import json

    from trading_pipeline.backtest.runner import fetch_backtest_bars
    from trading_pipeline.backtest.sweep import (
        SweepTarget,
        parse_grid,
        run_sweep,
        valid_combinations,
    )

    strategy = args.strategy
    grid = parse_grid(strategy, args.grid)
    split = _parse_date(args.split) or datetime(2024, 1, 1, tzinfo=UTC)
    start = start or split - timedelta(days=3 * 365)
    if start >= split or (end is not None and split >= end):
        raise ValueError("--sweep needs --start < --split < --end.")

    candidates = valid_combinations(strategy, base_params, grid) or [base_params]
    built = [get_strategy(strategy, params) for params in candidates]
    min_bars = max([s.min_bars for s in built] + [get_strategy("sma_rsi").min_bars])
    print(
        f"Sweeping {strategy}: {len(candidates)} combinations x {len(items)} entries, "
        f"in-sample {start:%Y-%m-%d}..{split:%Y-%m-%d}, out-of-sample {split:%Y-%m-%d}.."
        f"{(end or datetime.now(UTC)):%Y-%m-%d}"
    )

    def fetch(symbol, item):
        return fetch_backtest_bars(
            broker, symbol, item.asset_class, item.timeframe, min_bars=min_bars,
            limit=item.limit, start=start, end=end,
        )

    benchmark_cache: dict = {}
    targets = []
    for item in items:
        benchmark = next(
            (b for s in built if (b := s.benchmark_for(item.symbol, item.asset_class))), None
        )
        benchmark_bars = None
        if benchmark:
            key = (benchmark, item.asset_class, item.timeframe)
            if key not in benchmark_cache:
                benchmark_cache[key] = fetch(benchmark, item)
            benchmark_bars = benchmark_cache[key]
        targets.append(
            SweepTarget(
                symbol=item.symbol,
                asset_class=item.asset_class,
                timeframe=item.timeframe,
                bars=fetch(item.symbol, item),
                benchmark_bars=benchmark_bars,
                notional=item.notional,
                risk_per_trade=item.risk_per_trade if item.sizing.mode.value == "risk" else None,
                max_notional=item.max_notional,
            )
        )

    ranked, out_rows = run_sweep(
        targets, strategy, grid, start, split, end, base_params=base_params, cash=args.cash,
        top=args.top, min_trades=args.min_trades, progress=logger.info,
    )
    grid_keys = list(grid)
    print(f"\nIn-sample ranking (by total P/L across entries, top 10 of {len(ranked)}):")
    _print_sweep_rows(ranked[:10], grid_keys, out_of_sample=False)
    print(f"\nOut-of-sample {split:%Y-%m-%d} onwards (not used for choosing):")
    _print_sweep_rows(out_rows, grid_keys, out_of_sample=True)

    path = Path("data/sweeps") / f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{strategy}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["phase", "label", "strategy", "params", "trades", "win_rate_pct", "pnl_usd",
                         "avg_trade_pct", "profit_factor", "avg_buy_hold_pct", "errors"])
        phases = [("in_sample", row, row.in_sample) for row in ranked]
        phases += [("out_of_sample", row, row.out_of_sample) for row in out_rows]
        for phase, row, summary in phases:
            writer.writerow([
                phase, row.label, row.strategy, json.dumps(row.params, sort_keys=True), summary.trades,
                summary.win_rate, round(summary.pnl, 2), summary.avg_trade_pct, summary.profit_factor,
                summary.avg_buy_hold_pct, "; ".join(summary.errors),
            ])
    print(f"\nAll results: {path}")
    return 0


def _print_sweep_rows(rows, grid_keys: list[str], out_of_sample: bool) -> None:
    def params_text(row) -> str:
        if row.label == "sma_rsi baseline":
            return "sma_rsi, default parameters"
        if row.label == "defaults":
            return "default parameters"
        return " ".join(f"{key}={row.params[key]}" for key in grid_keys if key in row.params)

    width = min(110, max([len("params")] + [len(params_text(row)) for row in rows]))
    header = f"{'':<18} {'params':<{width}} {'trades':>6} {'win %':>6} {'P/L $':>9} {'avg %':>6} {'PF':>5}"
    if out_of_sample:
        header += f" {'in P/L $':>9} {'B&H %':>7}"
    print(header)
    print("-" * len(header))
    for row in rows:
        summary = row.out_of_sample if out_of_sample else row.in_sample
        line = (
            f"{row.label:<18} {params_text(row)[:width]:<{width}} {summary.trades:>6} "
            f"{_fmt(summary.win_rate, 1):>6} {summary.pnl:>9.2f} {_fmt(summary.avg_trade_pct, 2):>6} "
            f"{_fmt(summary.profit_factor, 2):>5}"
        )
        if out_of_sample:
            line += f" {row.in_sample.pnl:>9.2f} {_fmt(summary.avg_buy_hold_pct, 1):>7}"
        print(line)
        for error in summary.errors[:3]:
            print(f"{'':<18} ! {error}")


def _fmt(value, decimals: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if number != number else f"{number:.{decimals}f}"


if __name__ == "__main__":
    sys.exit(main())
