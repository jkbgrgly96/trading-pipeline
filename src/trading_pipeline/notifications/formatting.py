from __future__ import annotations

from datetime import datetime

from trading_pipeline.domain import Decision, DecisionAction, Signal


def format_signal(signal: Signal) -> str:
    lines = [
        f"{signal.symbol}: {signal.action.value.upper()}",
        f"close={signal.latest_close:.4f}",
        f"confidence={signal.confidence:.2f}",
        f"time={signal.timestamp.isoformat()}",
        f"reason={signal.reason}",
    ]
    if signal.stop_loss is not None:
        lines.append(f"stop_loss={signal.stop_loss:.4f}")
    if signal.take_profit is not None:
        lines.append(f"take_profit={signal.take_profit:.4f}")
    return "\n".join(lines)


def format_decision(
    decision: Decision,
    timeframe: str,
    order_line: str,
    strategy: str | None = None,
) -> str:
    signal = decision.signal
    header_context = (
        f"{timeframe} · {signal.asset_class.value} · bar {_bar_time(signal.timestamp, timeframe)}"
    )
    if strategy:
        header_context += f" · {strategy}"

    if decision.action == DecisionAction.ENTER_LONG:
        lines = [
            f"🟢 BUY {signal.symbol} — enter long",
            header_context,
            f"Price: {_price(decision.price)}",
        ]
        if decision.stop_loss is not None and decision.take_profit is not None:
            lines.append(
                f"Stop: {_price(decision.stop_loss)} ({_pct(decision.stop_loss, decision.price)})"
                f"   Target: {_price(decision.take_profit)}"
                f" ({_pct(decision.take_profit, decision.price)})"
            )
        lines += [
            f"Why: {decision.reason}",
            f"Confidence: {signal.confidence:.2f}",
        ]
    elif decision.action == DecisionAction.EXIT_LONG:
        lines = [
            f"🔴 SELL {signal.symbol} — close long",
            header_context,
            f"Price: {_price(decision.price)}",
        ]
        position = decision.position
        if position is not None:
            lines.append(
                f"Entry: {_price(position.entry_price)} on "
                f"{_bar_time(position.entry_time, timeframe)}"
                f"   P/L: {_pct(decision.price, position.entry_price)}"
            )
        lines.append(f"Why: {decision.reason}")
    else:
        lines = [
            f"⚪ HOLD {signal.symbol} — no action",
            header_context,
            f"Price: {_price(decision.price)}",
            f"Why: {decision.reason}",
        ]

    lines.append(order_line)
    return "\n".join(lines)


def format_info(title: str, body: str) -> str:
    return f"ℹ️ {title}\n{body}"


def format_error(context: str, error: str) -> str:
    return f"⚠️ Pipeline error — {context}\n{error}"


def _bar_time(value: datetime, timeframe: str) -> str:
    if timeframe.lower().endswith("day") or timeframe.lower().endswith("d"):
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.6f}"


def _pct(value: float, reference: float) -> str:
    if not reference:
        return "n/a"
    return f"{(value / reference - 1) * 100:+.2f}%"
