"""Run the pipeline continuously over a watchlist and alert on decisions."""

from __future__ import annotations

import logging
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_pipeline.brokers.base import Broker
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications.base import Notifier
from trading_pipeline.notifications.formatting import format_error
from trading_pipeline.pipeline import InsufficientBarsError, flush_outbox, run_once, sync_order_fills
from trading_pipeline.storage import Storage
from trading_pipeline.strategies import DEFAULT_STRATEGY, Strategy, get_strategy
from trading_pipeline.timeframes import parse_timeframe

logger = logging.getLogger(__name__)

# Alpaca publishes a bar a few seconds after it closes.
BAR_PUBLISH_GRACE = timedelta(seconds=20)
MIN_INTERVAL = timedelta(minutes=1)
MAX_INTERVAL = timedelta(minutes=15)
ERROR_ALERT_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    asset_class: AssetClass
    timeframe: str
    limit: int = 250
    notional: float = 100.0
    strategy: str = DEFAULT_STRATEGY

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.timeframe} ({self.asset_class.value})"


def load_watchlist(
    path: str | Path,
    default_limit: int,
    default_notional: float,
    default_strategy: str = DEFAULT_STRATEGY,
) -> list[WatchItem]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    limit = int(defaults.get("limit", default_limit))
    notional = float(defaults.get("notional", default_notional))
    strategy = str(defaults.get("strategy", default_strategy))
    items = []
    for entry in data.get("watch", []):
        timeframe = str(entry.get("timeframe", defaults.get("timeframe", "1Day")))
        parse_timeframe(timeframe)  # fail fast on typos
        item_strategy = str(entry.get("strategy", strategy))
        get_strategy(item_strategy)  # fail fast on unknown names
        items.append(
            WatchItem(
                symbol=str(entry["symbol"]),
                asset_class=AssetClass(entry.get("asset_class", defaults.get("asset_class", "stock"))),
                timeframe=timeframe,
                limit=int(entry.get("limit", limit)),
                notional=float(entry.get("notional", notional)),
                strategy=item_strategy,
            )
        )
    if not items:
        raise ValueError(f"No [[watch]] entries in {path}.")
    return items


def validate_watchlist(items: list[WatchItem], orders_enabled: bool) -> None:
    keys = [(item.symbol, item.asset_class, item.timeframe) for item in items]
    if len(set(keys)) != len(keys):
        # Stored signals and positions are keyed by symbol/asset_class/timeframe,
        # so two strategies cannot share one.
        raise ValueError("Watchlist contains duplicate symbol/asset_class/timeframe entries.")
    for item in items:
        min_bars = get_strategy(item.strategy).min_bars
        if item.limit < min_bars:
            raise ValueError(
                f"{item.label}: limit {item.limit} is below the {min_bars} bars "
                f"{item.strategy} needs."
            )
    if orders_enabled:
        symbols = [item.symbol for item in items]
        duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
        if duplicates:
            # The paper account holds one position per symbol, so two
            # timeframes trading the same symbol would fight over it.
            raise ValueError(
                "With paper orders enabled each symbol may appear only once; "
                f"duplicates: {', '.join(duplicates)}"
            )


def default_interval(items: list[WatchItem]) -> timedelta:
    shortest = min(parse_timeframe(item.timeframe).duration for item in items)
    return max(MIN_INTERVAL, min(shortest, MAX_INTERVAL))


def next_run_time(now: datetime, interval: timedelta) -> datetime:
    """Next interval boundary (aligned to the epoch) plus the publish grace."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    seconds = interval.total_seconds()
    elapsed = (now - epoch).total_seconds()
    boundary = epoch + timedelta(seconds=(elapsed // seconds + 1) * seconds)
    return boundary + BAR_PUBLISH_GRACE


class Watcher:
    def __init__(
        self,
        broker: Broker,
        storage: Storage,
        notifier: Notifier,
        items: list[WatchItem],
        orders_enabled: bool,
        notify_hold: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        max_open_positions: int | None = None,
    ) -> None:
        validate_watchlist(items, orders_enabled)
        if max_open_positions is not None and max_open_positions < 0:
            raise ValueError("max_open_positions must be >= 0 (0 or None: no limit).")
        self.max_open_positions = max_open_positions or None
        self._strategies: dict[WatchItem, Strategy] = {
            item: get_strategy(item.strategy) for item in items
        }
        self.broker = broker
        self.storage = storage
        self.notifier = notifier
        self.items = items
        self.orders_enabled = orders_enabled
        self.notify_hold = notify_hold
        self._clock = clock
        self._sleep = sleep
        self._last_error_alert: dict[str, datetime] = {}

    def run_cycle(self) -> dict[str, str]:
        """Process every watchlist item once. Never raises for per-symbol errors."""
        outcomes: dict[str, str] = {}
        try:
            flush_outbox(self.storage, self.notifier, now=self._clock())
        except Exception:  # noqa: BLE001 - never let the outbox stop alerting
            logger.exception("Outbox flush failed")
        if self.orders_enabled:
            try:
                sync_order_fills(self.storage, self.broker, now=self._clock())
            except Exception:  # noqa: BLE001 - fills are bookkeeping, never block alerts
                logger.exception("Fill sync failed")
        for item in self.items:
            outcomes[item.label] = self._run_item(item)
        return outcomes

    def run_forever(self, interval: timedelta | None = None, max_cycles: int | None = None) -> None:
        interval = interval or default_interval(self.items)
        mode = "PAPER ORDERS ON" if self.orders_enabled else "alerts only"
        if self.max_open_positions:
            mode += f", max {self.max_open_positions} open positions"
        symbols = ", ".join(f"{item.label} [{item.strategy}]" for item in self.items)
        self._safe_notify(
            f"▶️ Watcher started ({mode}), every {interval}.\nWatching: {symbols}"
        )
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                try:
                    outcomes = self.run_cycle()
                    logger.info("Cycle done: %s", outcomes)
                except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                    logger.exception("Watch cycle failed")
                    self._alert_error("watcher", f"{type(exc).__name__}: {exc}")
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                wake = next_run_time(self._clock(), interval)
                self._sleep(max(0.0, (wake - self._clock()).total_seconds()))
        except KeyboardInterrupt:
            logger.info("Watcher stopped by user.")
        finally:
            self._safe_notify("⏹️ Watcher stopped.")

    def _run_item(self, item: WatchItem) -> str:
        try:
            result = run_once(
                broker=self.broker,
                notifier=self.notifier,
                symbol=item.symbol,
                asset_class=item.asset_class,
                timeframe=item.timeframe,
                limit=item.limit,
                orders_enabled=self.orders_enabled,
                notional=item.notional,
                storage=self.storage,
                notify_hold=self.notify_hold,
                now=self._clock(),
                strategy=self._strategies[item],
                max_open_positions=self.max_open_positions,
            )
        except InsufficientBarsError as exc:
            logger.warning("%s", exc)
            self._record_error(item, str(exc), alert=False)
            return "insufficient_bars"
        except Exception as exc:  # noqa: BLE001 - keep watching other symbols
            logger.exception("Pipeline failed for %s", item.label)
            self._record_error(item, f"{type(exc).__name__}: {exc}", alert=True)
            return "error"
        if result.status == "processed" and result.decision is not None:
            return result.decision.action.value
        return result.status

    def _record_error(self, item: WatchItem, error: str, alert: bool) -> None:
        try:
            self.storage.record_run(item.symbol, item.asset_class, item.timeframe, None, error)
        except Exception:  # noqa: BLE001
            logger.exception("Could not record error status")
        if alert:
            self._alert_error(item.label, error)

    def _alert_error(self, label: str, error: str) -> None:
        now = self._clock()
        last = self._last_error_alert.get(label)
        if last is None or now - last >= ERROR_ALERT_COOLDOWN:
            self._last_error_alert[label] = now
            self._safe_notify(format_error(label, error[:500]))

    def _safe_notify(self, text: str) -> None:
        try:
            self.notifier.send_message(text)
        except Exception:  # noqa: BLE001
            logger.exception("Status notification failed")
