"""Turn a strategy signal plus position state into an actionable decision.

This is the single source of truth for "should I enter / exit now?" and is
used by both the live pipeline and the backtest, so the two cannot drift.

Exits are evaluated on the *closed* bar, the same moment an alert can be
sent: if the bar's low touched the stop or its high touched the target, the
position should be closed. The backtest then fills on the next bar's open
(or this bar's close with ``--trade-on-close``), which is what a person
acting on the alert would get.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading_pipeline.domain import (
    AssetClass,
    Decision,
    DecisionAction,
    ExitReason,
    OpenPosition,
    PositionPlan,
    Signal,
    SignalAction,
    StopTrigger,
)
from trading_pipeline.features import FeatureSnapshot
from trading_pipeline.strategies.base import History, Strategy


def evaluate(
    strategy: Strategy,
    symbol: str,
    asset_class: AssetClass,
    history: History,
    position: OpenPosition | None,
    entries_allowed: bool = True,
) -> Decision:
    """One closed bar, live or in the backtest: signal + position rules -> decision."""
    signal = strategy.signal(symbol, asset_class, history)
    plan = strategy.manage(position, history) if position is not None else None
    return decide(
        signal,
        position,
        history.latest,
        entries_allowed=entries_allowed,
        plan=plan,
        stop_trigger=StopTrigger(strategy.stop_trigger),
    )


def decide(
    signal: Signal,
    position: OpenPosition | None,
    latest: FeatureSnapshot,
    entries_allowed: bool = True,
    plan: PositionPlan | None = None,
    stop_trigger: StopTrigger = StopTrigger.INTRABAR,
) -> Decision:
    """``entries_allowed=False`` blocks new entries (e.g. the open-position cap is
    reached); exits of an open position are never blocked.

    The stop in force for this bar is the position's stored one. A raised stop
    from ``plan`` applies from the next bar on (it is computed from this bar's
    data, so checking this bar against it would look ahead within the bar).
    """
    if position is None:
        if signal.action == SignalAction.BUY and not entries_allowed:
            return Decision(
                DecisionAction.NONE,
                f"buy_signal_skipped_max_open_positions, {signal.reason}",
                signal,
                latest.close,
            )
        if signal.action == SignalAction.BUY:
            return Decision(
                action=DecisionAction.ENTER_LONG,
                reason=signal.reason,
                signal=signal,
                price=latest.close,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
        reason = (
            "sell_signal_ignored_no_open_position"
            if signal.action == SignalAction.SELL
            else signal.reason
        )
        return Decision(DecisionAction.NONE, reason, signal, latest.close)

    # The high/low of the bar the position was opened on happened *before*
    # the entry (entries fill at/after that bar's close), so price levels are
    # only checked from the following bar on.
    levels_active = _as_utc(latest.timestamp) > _as_utc(position.entry_time)

    intrabar = stop_trigger == StopTrigger.INTRABAR
    stop_price, stop_label = (latest.low, "low") if intrabar else (latest.close, "close")
    target_price, target_label = (latest.high, "high") if intrabar else (latest.close, "close")

    # Stop is checked before target: if a single bar spans both, assume the
    # worse outcome (the conservative choice, also used by most backtesters).
    if levels_active and position.stop_loss is not None and stop_price <= position.stop_loss:
        return _exit(
            signal,
            position,
            latest,
            ExitReason.STOP_LOSS,
            f"{stop_label} {stop_price:.4f} <= stop {position.stop_loss:.4f}",
        )
    if levels_active and position.take_profit is not None and target_price >= position.take_profit:
        return _exit(
            signal,
            position,
            latest,
            ExitReason.TAKE_PROFIT,
            f"{target_label} {target_price:.4f} >= target {position.take_profit:.4f}",
        )
    if plan is not None and plan.exit_reason:
        return _exit(signal, position, latest, ExitReason.STRATEGY, plan.exit_reason)
    if signal.action == SignalAction.SELL:
        return _exit(signal, position, latest, ExitReason.SIGNAL, signal.reason)

    reason = (
        "buy_signal_ignored_already_in_position"
        if signal.action == SignalAction.BUY
        else f"holding_position, {signal.reason}"
    )
    return Decision(
        DecisionAction.NONE,
        reason,
        signal,
        latest.close,
        stop_loss=_raised_stop(position.stop_loss, plan.stop_loss if plan else None),
        take_profit=position.take_profit,
        position=position,
    )


def _raised_stop(current: float | None, proposed: float | None) -> float | None:
    """Trailing stops only move up (for a long): a lower proposal is ignored."""
    if proposed is None:
        return current
    return proposed if current is None else max(current, proposed)


def _exit(
    signal: Signal,
    position: OpenPosition,
    latest: FeatureSnapshot,
    exit_reason: ExitReason,
    detail: str,
) -> Decision:
    return Decision(
        action=DecisionAction.EXIT_LONG,
        reason=f"{exit_reason.value}: {detail}",
        signal=signal,
        price=latest.close,
        stop_loss=position.stop_loss,
        take_profit=position.take_profit,
        exit_reason=exit_reason,
        position=position,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
