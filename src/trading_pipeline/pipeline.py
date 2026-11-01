"""Per-symbol processing: closed bars -> features -> signal -> decision.

Guarantees the alerting use case relies on:

* Only completed bars are evaluated (no repainting signals).
* Every closed bar is evaluated exactly once, in order. If runs were missed
  (downtime, a late bar, an error), the missed bars are replayed on the next
  run so no crossover or stop/target touch is skipped; their alerts are
  marked as late.
* Each bar is stored under a unique key, so reruns and overlapping schedules
  never duplicate alerts or orders.
* Alerts are about *actions*: enter when flat, exit when long (sell signal,
  stop-loss or take-profit). Signals that need no action are stored but not
  sent unless ``notify_hold`` is set.
* Notifications go through an outbox: the message is stored first and a
  failed send is retried on later cycles for up to ``OUTBOX_MAX_AGE``.
* Orders are only ever sent to the Alpaca *paper* account, and only when
  ``orders_enabled`` (ALPACA_PAPER + TRADING_MODE=paper + PLACE_ORDERS).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pandas as pd

from trading_pipeline.brokers.base import DEAD_ORDER_STATUSES, Broker, is_order_open
from trading_pipeline.decisions import decide
from trading_pipeline.domain import (
    AssetClass,
    Bar,
    Decision,
    DecisionAction,
    OpenPosition,
    Signal,
)
from trading_pipeline.features import FeatureSnapshot, compute_feature_frame, snapshots_from_frame
from trading_pipeline.notifications.base import Notifier
from trading_pipeline.notifications.formatting import format_decision, format_info
from trading_pipeline.storage import Storage
from trading_pipeline.storage.database import iso_timestamp
from trading_pipeline.strategies.sma_rsi import (
    MIN_BARS,
    generate_signal_from_features,
    risk_levels,
)
from trading_pipeline.timeframes import closed_bars, parse_timeframe

logger = logging.getLogger(__name__)

# Bars replayed at most after a gap; older missed bars are skipped (logged).
MAX_CATCH_UP_BARS = 50
# Undelivered alerts are retried for this long, then reported as abandoned.
OUTBOX_MAX_AGE = timedelta(hours=24)
PAPER_SOURCES = frozenset({"paper", "paper_adopted"})


class InsufficientBarsError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineResult:
    status: str  # "processed" | "already_processed"
    bar_timestamp: datetime
    signal: Signal | None = None
    decision: Decision | None = None
    signal_id: int | None = None
    order_id: str | None = None
    order_error: str | None = None
    notified: bool = False
    bars_processed: int = 0


@dataclass(frozen=True)
class _Context:
    broker: Broker
    notifier: Notifier
    storage: Storage
    symbol: str
    asset_class: AssetClass
    timeframe: str
    orders_enabled: bool
    notional: float
    notify_hold: bool
    now: datetime


def run_once(
    broker: Broker,
    notifier: Notifier,
    symbol: str,
    asset_class: AssetClass,
    timeframe: str,
    limit: int,
    orders_enabled: bool,
    notional: float,
    storage: Storage,
    source: str = "alpaca",
    notify_hold: bool = False,
    now: datetime | None = None,
) -> PipelineResult:
    now = now or datetime.now(UTC)
    parsed = parse_timeframe(timeframe)
    raw_bars = broker.get_bars(
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        limit=limit,
    )
    bars = closed_bars(raw_bars, parsed, now)
    if len(bars) < MIN_BARS:
        raise InsufficientBarsError(
            f"{symbol} {timeframe}: {len(bars)} closed bars, need at least {MIN_BARS}. "
            "Increase --limit or check the symbol/feed."
        )

    pending = _pending_indexes(storage, symbol, asset_class, timeframe, bars)
    last_bar_time = bars[-1].timestamp
    if not pending:
        storage.record_run(symbol, asset_class, timeframe, last_bar_time)
        return PipelineResult(status="already_processed", bar_timestamp=last_bar_time)

    # Features are causal, so one frame over all closed bars gives the same
    # values per bar as computing each bar separately.
    feature_frame = compute_feature_frame(bars)
    snapshots = snapshots_from_frame(feature_frame)
    storage.save_market_bars(symbol, asset_class, timeframe, bars, source=source)
    storage.save_feature_snapshots(symbol, asset_class, timeframe, feature_frame)

    context = _Context(
        broker, notifier, storage, symbol, asset_class, timeframe,
        orders_enabled, notional, notify_hold, now,
    )
    result: PipelineResult | None = None
    for index in pending:
        late = index != len(bars) - 1
        result = _process_bar(context, snapshots[index], snapshots[index - 1], late)

    storage.record_run(symbol, asset_class, timeframe, last_bar_time)
    assert result is not None
    return replace(result, bars_processed=len(pending))


def _pending_indexes(
    storage: Storage,
    symbol: str,
    asset_class: AssetClass,
    timeframe: str,
    bars: list[Bar],
) -> list[int]:
    """Indexes of closed bars not processed yet, oldest first.

    The first run for a symbol only evaluates the latest bar (no alert storm
    from history). Later runs replay every bar after the last processed one.
    """
    first_usable = MIN_BARS - 1
    last_done = storage.last_processed_bar(symbol, asset_class, timeframe)
    if last_done is None:
        return [len(bars) - 1]
    last_done_key = iso_timestamp(last_done)
    pending = [
        index
        for index in range(first_usable, len(bars))
        if iso_timestamp(bars[index].timestamp) > last_done_key
    ]
    if len(pending) > MAX_CATCH_UP_BARS:
        logger.warning(
            "%s %s: %d bars missed; replaying only the last %d.",
            symbol, timeframe, len(pending), MAX_CATCH_UP_BARS,
        )
        pending = pending[-MAX_CATCH_UP_BARS:]
    return pending


def _process_bar(
    context: _Context,
    latest: FeatureSnapshot,
    previous: FeatureSnapshot,
    late: bool,
) -> PipelineResult:
    storage = context.storage
    position = _current_position(context, latest)
    signal = generate_signal_from_features(
        symbol=context.symbol,
        asset_class=context.asset_class,
        latest=latest,
        previous=previous,
    )
    decision = decide(signal, position, latest)
    notify_required = decision.is_actionable or (context.notify_hold and not late)

    signal_id = storage.save_signal(
        symbol=context.symbol,
        asset_class=context.asset_class,
        timeframe=context.timeframe,
        signal=signal,
        snapshot=latest,
        orders_enabled=context.orders_enabled,
        decision=decision,
        notify_required=notify_required,
        notify_message=_message(decision, context, "Paper order: pending", late),
    )
    if signal_id is None:
        # Another process claimed this bar between the check and the insert.
        return PipelineResult(status="already_processed", bar_timestamp=latest.timestamp)

    order_id, order_error = _apply_decision(context, decision, signal_id)
    message = _message(
        decision,
        context,
        _order_line(decision, context.orders_enabled, order_id, order_error),
        late,
    )
    storage.set_notify_message(signal_id, message)
    notified = False
    if notify_required:
        notified = deliver(storage, context.notifier, signal_id, message)

    return PipelineResult(
        status="processed",
        bar_timestamp=latest.timestamp,
        signal=signal,
        decision=decision,
        signal_id=signal_id,
        order_id=order_id,
        order_error=order_error,
        notified=notified,
    )


def _message(decision: Decision, context: _Context, order_line: str, late: bool) -> str:
    text = format_decision(decision, context.timeframe, order_line)
    if late:
        text = "(late: bar was missed, replayed now)\n" + text
    return text


def deliver(storage: Storage, notifier: Notifier, signal_id: int, message: str) -> bool:
    try:
        notifier.send_message(message)
    except Exception as exc:  # noqa: BLE001 - recorded and retried by the outbox
        logger.warning("Notification for signal %s failed: %s", signal_id, exc)
        storage.mark_notify_failed(signal_id, str(exc))
        return False
    storage.mark_notified(signal_id)
    return True


def flush_outbox(
    storage: Storage,
    notifier: Notifier,
    now: datetime | None = None,
    max_age: timedelta = OUTBOX_MAX_AGE,
) -> int:
    """Retry alerts that failed earlier. Returns how many were sent.

    Alerts older than ``max_age`` are given up on, and a single summary
    message tells the user how many were lost so it never happens silently.
    """
    now = now or datetime.now(UTC)
    sent = 0
    abandoned: list[str] = []
    for row in storage.pending_notifications():
        signal_id = int(row["id"])
        created = pd.Timestamp(row["created_at"]).to_pydatetime()
        if now - created > max_age:
            storage.mark_notify_abandoned(signal_id)
            abandoned.append(f"{row['symbol']} {row['decision']} (bar {row['bar_timestamp'][:16]})")
            continue
        message = row.get("notify_message")
        if message and deliver(storage, notifier, signal_id, f"(delayed) {message}"):
            sent += 1
    if abandoned:
        _notify_best_effort(
            notifier,
            f"⚠️ {len(abandoned)} alert(s) could not be delivered for {max_age} "
            f"and were dropped: " + "; ".join(abandoned[:10]) + ". Check the dashboard.",
        )
    return sent


def _current_position(context: _Context, latest: FeatureSnapshot) -> OpenPosition | None:
    """Local position, reconciled with the paper account when orders are on.

    Only positions that came from the paper account are reconciled; virtual
    positions (from alerts-only mode) are left alone so their exit alerts
    still arrive after paper orders are switched on.
    """
    storage, broker = context.storage, context.broker
    local = storage.get_open_position(context.symbol, context.asset_class, context.timeframe)
    if not context.orders_enabled:
        return local

    remote = broker.get_position(context.symbol, context.asset_class)
    if remote is not None and local is None:
        if _exit_order_pending(context):
            # We already sold; the paper account just hasn't filled it yet.
            return None
        stop_loss, take_profit = risk_levels(remote.avg_entry_price)
        storage.open_position(
            symbol=context.symbol,
            asset_class=context.asset_class,
            timeframe=context.timeframe,
            entry_price=remote.avg_entry_price,
            # Treated as entered at this bar's close: its high/low predate us.
            entry_time=latest.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            source="paper_adopted",
            quantity=remote.quantity,
        )
        _notify_best_effort(
            context.notifier,
            format_info(
                f"Tracking paper position {context.symbol}",
                f"Found {remote.quantity:g} @ {remote.avg_entry_price:.4f} in the paper account. "
                f"Stop {stop_loss:.4f}, target {take_profit:.4f}.",
            ),
        )
        return storage.get_open_position(context.symbol, context.asset_class, context.timeframe)

    if remote is None and local is not None and local.source in PAPER_SOURCES:
        status = broker.get_order_status(local.entry_order_id) if local.entry_order_id else None
        if is_order_open(status):
            return local  # entry still queued, e.g. submitted after hours
        reason = f"entry_order_{status}" if status in DEAD_ORDER_STATUSES else "closed_outside_pipeline"
        assert local.id is not None
        storage.close_position(local.id, latest.close, latest.timestamp, reason)
        _notify_best_effort(
            context.notifier,
            format_info(
                f"Stopped tracking {context.symbol}",
                f"The paper account has no {context.symbol} position ({reason}). "
                "No exit alert will follow for it.",
            ),
        )
        return None

    return local


def _exit_order_pending(context: _Context) -> bool:
    for order_id in context.storage.recent_exit_order_ids(context.symbol, context.asset_class):
        if is_order_open(context.broker.get_order_status(order_id)):
            return True
    return False


def _apply_decision(
    context: _Context,
    decision: Decision,
    signal_id: int,
) -> tuple[str | None, str | None]:
    storage, broker = context.storage, context.broker
    signal = decision.signal
    if decision.action == DecisionAction.ENTER_LONG:
        position_id = storage.open_position(
            symbol=signal.symbol,
            asset_class=signal.asset_class,
            timeframe=context.timeframe,
            entry_price=decision.price,
            entry_time=signal.timestamp,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            source="paper" if context.orders_enabled else "virtual",
            entry_signal_id=signal_id,
        )
        storage.set_signal_position(signal_id, position_id)
        if not context.orders_enabled:
            return None, None
        try:
            order_id = broker.submit_market_buy(signal.symbol, signal.asset_class, context.notional)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Paper buy for %s failed", signal.symbol)
            storage.update_signal_order(signal_id, None, "failed", str(exc))
            storage.close_position(position_id, None, signal.timestamp, "entry_order_failed")
            return None, str(exc)
        storage.update_signal_order(signal_id, order_id, "submitted")
        storage.set_position_entry_order(position_id, order_id)
        return order_id, None

    if decision.action == DecisionAction.EXIT_LONG:
        position = decision.position
        assert position is not None and position.id is not None
        storage.set_signal_position(signal_id, position.id)
        order_id = None
        if context.orders_enabled:
            try:
                # A still-queued entry would fill after we "closed"; cancel it.
                if position.entry_order_id and is_order_open(
                    broker.get_order_status(position.entry_order_id)
                ):
                    broker.cancel_order(position.entry_order_id)
                order_id = broker.close_position(signal.symbol, signal.asset_class)
            except Exception as exc:  # noqa: BLE001
                # Keep the position open so the next bar re-evaluates the exit.
                logger.exception("Paper close for %s failed", signal.symbol)
                storage.update_signal_order(signal_id, None, "failed", str(exc))
                return None, str(exc)
            storage.update_signal_order(
                signal_id, order_id, "submitted" if order_id else "already_flat"
            )
        exit_reason = decision.exit_reason.value if decision.exit_reason else "exit"
        storage.close_position(
            position.id,
            decision.price,
            signal.timestamp,
            exit_reason,
            exit_signal_id=signal_id,
            exit_order_id=order_id,
        )
        return order_id, None

    return None, None


def _notify_best_effort(notifier: Notifier, text: str) -> None:
    try:
        notifier.send_message(text)
    except Exception:  # noqa: BLE001 - informational only
        logger.exception("Informational notification failed")


def _order_line(
    decision: Decision,
    orders_enabled: bool,
    order_id: str | None,
    order_error: str | None,
) -> str:
    if not decision.is_actionable:
        return "Paper order: none"
    if not orders_enabled:
        return "Paper order: not placed (alerts only)"
    if order_error:
        return f"Paper order: FAILED — {order_error[:200]}"
    if order_id:
        return f"Paper order: submitted ({order_id})"
    return "Paper order: nothing to close (paper account already flat)"
