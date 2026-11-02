from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from trading_pipeline.config import load_settings
from trading_pipeline.dashboard import queries
from trading_pipeline.dashboard.charts import (
    build_drawdown_chart,
    build_equity_curve_chart,
    build_market_chart,
    build_trade_overlay_chart,
    build_trade_pnl_chart,
)
from trading_pipeline.dashboard.formatting import (
    compact_dict,
    format_number,
    format_percent,
    parse_json,
)

DECISION_LABELS = {"enter_long": "BUY / enter", "exit_long": "SELL / exit"}


def render() -> None:
    import streamlit as st

    st.set_page_config(page_title="Trading Pipeline", layout="wide")
    st.title("Trading Pipeline")

    settings = load_settings()
    with st.sidebar:
        st.header("Data")
        database_path = st.text_input("Database path", settings.database_path)
        db_path = Path(database_path)
        auto_refresh = st.toggle("Auto refresh", value=True)
        refresh_seconds = st.select_slider(
            "Refresh every (s)", options=[15, 30, 60, 120, 300], value=60, disabled=not auto_refresh
        )
        if st.button("Refresh now"):
            st.rerun()

    if not db_path.exists():
        st.warning(f"Database not found: {db_path}")
        st.stop()

    symbols = queries.load_symbols(db_path)
    if symbols.empty:
        st.info("No stored market data, signals, positions, or backtests yet.")
        st.stop()

    selected = _sidebar_filters(st, symbols)
    start_iso = selected["start"].isoformat() if selected["start"] else None
    end_iso = (selected["end"] + timedelta(days=1)).isoformat() if selected["end"] else None

    @st.fragment(run_every=refresh_seconds if auto_refresh else None)
    def live_view() -> None:
        tabs = st.tabs(["Overview", "Decisions", "Positions", "Chart", "Signals", "Backtests"])
        with tabs[0]:
            _render_overview(st, db_path)
        with tabs[1]:
            _render_decisions(st, db_path, selected, start_iso, end_iso)
        with tabs[2]:
            _render_positions(st, db_path, selected)
        with tabs[3]:
            _render_chart(st, db_path, selected, start_iso, end_iso)
        with tabs[4]:
            _render_signals(st, db_path, selected, start_iso, end_iso)
        with tabs[5]:
            _render_backtests(st, db_path, selected, start_iso, end_iso)

    live_view()


def _sidebar_filters(st, symbols: pd.DataFrame) -> dict[str, object]:
    with st.sidebar:
        st.header("Filters")
        symbol_options = symbols["symbol"].dropna().drop_duplicates().tolist()
        symbol = st.selectbox("Symbol", symbol_options)

        scoped = symbols[symbols["symbol"] == symbol]
        asset_options = scoped["asset_class"].dropna().drop_duplicates().tolist()
        asset_class = st.selectbox("Asset class", asset_options)

        scoped = scoped[scoped["asset_class"] == asset_class]
        timeframe_options = scoped["timeframe"].dropna().drop_duplicates().tolist()
        timeframe = st.selectbox("Timeframe", timeframe_options)

        default_end = date.today()
        default_start = default_end - timedelta(days=365)
        date_range = st.date_input("Date range", value=(default_start, default_end))
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start, end = date_range
        else:
            start, end = default_start, default_end

        action = st.selectbox("Signal action", ["all", "buy", "sell", "hold"])

    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "action": None if action == "all" else action,
    }


# --- Overview -----------------------------------------------------------


def _render_overview(st, database_path: Path) -> None:
    overview = queries.load_overview(database_path)
    status = queries.load_symbol_status(database_path)
    open_positions = queries.load_positions(database_path, status="open")
    latest_decision = overview["latest_decision"]

    columns = st.columns(4)
    columns[0].metric("Open positions", overview["open_positions"])
    if latest_decision:
        columns[1].metric(
            "Last alert",
            f"{DECISION_LABELS.get(latest_decision['decision'], latest_decision['decision'])}",
            f"{latest_decision['symbol']} · {str(latest_decision['bar_timestamp'])[:10]}",
            delta_color="off",
        )
    else:
        columns[1].metric("Last alert", "none yet")
    healthy = int((status["health"] == "ok").sum()) if not status.empty else 0
    columns[2].metric("Watched symbols OK", f"{healthy}/{len(status)}")
    pending = overview["pending_notifications"]
    columns[3].metric("Unsent alerts", pending, delta="needs attention" if pending else None, delta_color="inverse")

    st.subheader("Open positions")
    if open_positions.empty:
        st.caption("Flat — no open positions.")
    else:
        st.dataframe(
            _select_columns(
                open_positions,
                [
                    "symbol",
                    "timeframe",
                    "source",
                    "entry_time",
                    "entry_price",
                    "mark_price",
                    "pnl_pct",
                    "stop_loss",
                    "to_stop_pct",
                    "take_profit",
                    "to_target_pct",
                    "last_bar_timestamp",
                ],
            ),
            column_config=_position_columns(st),
            hide_index=True,
            width="stretch",
        )

    st.subheader("Recent alerts")
    decisions = queries.load_decisions(database_path).head(10)
    if decisions.empty:
        st.caption("No entry/exit decisions yet. HOLD bars are in the Signals tab.")
    else:
        st.dataframe(
            _decision_table(decisions),
            column_config=_decision_columns(st),
            hide_index=True,
            width="stretch",
        )

    st.subheader("Watcher health")
    if status.empty:
        st.caption("No runs recorded yet. Start `trading-pipeline --watch`.")
    else:
        st.dataframe(
            _select_columns(
                status,
                [
                    "health",
                    "symbol",
                    "asset_class",
                    "timeframe",
                    "last_run_at",
                    "last_bar_timestamp",
                    "last_error",
                    "last_error_at",
                ],
            ),
            hide_index=True,
            width="stretch",
        )

    with st.expander("Database contents"):
        st.json(overview["table_counts"])


# --- Decisions ----------------------------------------------------------


def _render_decisions(st, database_path: Path, selected: dict, start: str | None, end: str | None) -> None:
    only_selected = st.checkbox("Only the selected symbol", value=False, key="decisions_scope")
    decisions = queries.load_decisions(
        database_path,
        symbol=selected["symbol"] if only_selected else None,
        asset_class=selected["asset_class"] if only_selected else None,
        timeframe=selected["timeframe"] if only_selected else None,
        start=start,
        end=end,
    )
    columns = st.columns(3)
    columns[0].metric("Alerts in range", len(decisions))
    if not decisions.empty:
        columns[1].metric("Entries", int((decisions["decision"] == "enter_long").sum()))
        columns[2].metric("Exits", int((decisions["decision"] == "exit_long").sum()))
        st.dataframe(
            _decision_table(decisions),
            column_config=_decision_columns(st),
            hide_index=True,
            width="stretch",
        )
        selected_id = st.selectbox(
            "Alert details",
            decisions["id"].tolist(),
            format_func=lambda value: _decision_label(decisions, value),
        )
        row = decisions[decisions["id"] == selected_id].iloc[0]
        detail = st.columns(2)
        with detail[0]:
            st.markdown("**Message sent**")
            st.code(row.get("notify_message") or "-", language=None)
        with detail[1]:
            st.markdown("**Indicators on that bar**")
            features = row.get("features")
            st.json(compact_dict(features if isinstance(features, dict) else None, _FEATURE_KEYS))
    st.download_button(
        "Download alerts CSV",
        data=decisions.to_csv(index=False),
        file_name="alerts.csv",
        mime="text/csv",
        disabled=decisions.empty,
    )


# --- Positions ----------------------------------------------------------


def _render_positions(st, database_path: Path, selected: dict) -> None:
    only_selected = st.checkbox("Only the selected symbol", value=False, key="positions_scope")
    scope = {
        "symbol": selected["symbol"] if only_selected else None,
        "asset_class": selected["asset_class"] if only_selected else None,
        "timeframe": selected["timeframe"] if only_selected else None,
    }
    positions = queries.load_positions(database_path, **scope)
    if positions.empty:
        st.info("No positions yet. They open on BUY alerts (virtual) or paper fills.")
        return

    closed = positions[positions["status"] == "closed"]
    realized = pd.to_numeric(closed["pnl_pct"], errors="coerce").dropna()
    columns = st.columns(4)
    columns[0].metric("Open", int((positions["status"] == "open").sum()))
    columns[1].metric("Closed", len(closed))
    columns[2].metric("Win rate", format_percent((realized > 0).mean() * 100 if len(realized) else None, 0))
    columns[3].metric("Avg closed P/L", format_percent(realized.mean() if len(realized) else None))

    st.dataframe(
        _select_columns(
            positions,
            [
                "status",
                "symbol",
                "timeframe",
                "strategy",
                "source",
                "entry_time",
                "entry_price",
                "exit_time",
                "mark_price",
                "pnl_pct",
                "exit_reason",
                "stop_loss",
                "take_profit",
                "entry_order_id",
                "exit_order_id",
            ],
        ),
        column_config=_position_columns(st),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Virtual positions assume you acted on every alert at the bar close. "
        "With paper orders on, positions are reconciled with the Alpaca paper account."
    )


# --- Chart --------------------------------------------------------------


def _render_chart(st, database_path: Path, selected: dict, start: str | None, end: str | None) -> None:
    symbol, asset_class, timeframe = selected["symbol"], selected["asset_class"], selected["timeframe"]
    options = st.columns(8)
    show_sma = options[0].checkbox("SMA", value=True)
    show_bbands = options[1].checkbox("Bollinger", value=False)
    show_signals = options[2].checkbox("Signals", value=True)
    show_hold = options[3].checkbox("HOLD", value=False)
    show_positions = options[4].checkbox("Positions", value=True)
    show_rsi = options[5].checkbox("RSI", value=True)
    show_macd = options[6].checkbox("MACD", value=False)
    show_atr = options[7].checkbox("ATR", value=False)

    bars = queries.load_market_bars(database_path, symbol, asset_class, timeframe, start, end)
    features = queries.load_feature_snapshots(database_path, symbol, asset_class, timeframe, start, end)
    signals = queries.load_signals(database_path, symbol, asset_class, timeframe, start=start, end=end)
    positions = queries.load_positions(
        database_path, symbol=symbol, asset_class=asset_class, timeframe=timeframe
    )

    if bars.empty:
        st.info("No market bars match the selected filters.")
        return

    figure = build_market_chart(
        bars=bars,
        features=features,
        signals=signals,
        show_sma=show_sma,
        show_bbands=show_bbands,
        show_signals=show_signals,
        show_hold_signals=show_hold,
        show_rsi=show_rsi,
        show_macd=show_macd,
        show_atr=show_atr,
        positions=positions,
        show_positions=show_positions,
    )
    st.plotly_chart(figure, width="stretch")

    summary_columns = st.columns(4)
    summary_columns[0].metric("Bars", len(bars))
    summary_columns[1].metric("Signals", len(signals))
    summary_columns[2].metric("First bar", _date_text(bars["timestamp"].min()))
    summary_columns[3].metric("Last bar", _date_text(bars["timestamp"].max()))

    if not features.empty:
        latest = features.iloc[-1]
        st.subheader("Latest feature snapshot")
        metric_columns = st.columns(6)
        metric_columns[0].metric("Close", format_number(bars.iloc[-1]["close"]))
        metric_columns[1].metric("RSI 14", format_number(latest.get("rsi_14")))
        metric_columns[2].metric("SMA 20", format_number(latest.get("sma_20")))
        metric_columns[3].metric("SMA 50", format_number(latest.get("sma_50")))
        metric_columns[4].metric("MACD Hist", format_number(latest.get("macd_hist")))
        metric_columns[5].metric("ATR 14", format_number(latest.get("atr_14")))
        st.caption(latest.get("patterns_text") or "No candle pattern on latest bar")


# --- Signals ------------------------------------------------------------


def _render_signals(st, database_path: Path, selected: dict, start: str | None, end: str | None) -> None:
    st.caption("Every processed bar, including HOLDs. Alerts are the rows with a decision.")
    signals = queries.load_signals(
        database_path,
        symbol=selected["symbol"],
        asset_class=selected["asset_class"],
        timeframe=selected["timeframe"],
        action=selected["action"],
        start=start,
        end=end,
    )
    st.metric("Matching signals", len(signals))
    display_columns = [
        "bar_timestamp",
        "action",
        "decision",
        "decision_reason",
        "confidence",
        "latest_close",
        "reason",
        "patterns_text",
        "rsi_14",
        "sma_20",
        "sma_50",
        "order_id",
        "order_status",
    ]
    st.dataframe(_select_columns(signals, display_columns), hide_index=True, width="stretch")
    if not signals.empty:
        selected_signal_id = st.selectbox("Signal details", signals["id"].tolist())
        selected_signal = signals[signals["id"] == selected_signal_id].iloc[0]
        with st.expander("Raw signal row"):
            st.json(_jsonable_row(selected_signal))

    st.download_button(
        "Download signals CSV",
        data=signals.to_csv(index=False),
        file_name="signals.csv",
        mime="text/csv",
        disabled=signals.empty,
    )


# --- Backtests ----------------------------------------------------------


def _render_backtests(st, database_path: Path, selected: dict, start: str | None, end: str | None) -> None:
    runs = queries.load_backtest_runs(
        database_path,
        symbol=selected["symbol"],
        asset_class=selected["asset_class"],
        timeframe=selected["timeframe"],
        start=start,
        end=end,
    )
    if runs.empty:
        st.info("No backtest runs match the selected filters.")
        return

    display_columns = [
        "id",
        "created_at",
        "symbol",
        "timeframe",
        "strategy",
        "bar_limit",
        "commission",
        "spread",
        "return_pct",
        "buy_hold_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "trade_count",
        "win_rate_pct",
    ]
    st.dataframe(_select_columns(runs, display_columns), hide_index=True, width="stretch")

    run_id = st.selectbox("Backtest run", runs["id"].tolist())
    selected_run = runs[runs["id"] == run_id].iloc[0]
    metrics = st.columns(5)
    metrics[0].metric("Return", format_percent(selected_run.get("return_pct")))
    metrics[1].metric("Buy & Hold", format_percent(selected_run.get("buy_hold_return_pct")))
    metrics[2].metric("Max DD", format_percent(selected_run.get("max_drawdown_pct")))
    metrics[3].metric("Sharpe", format_number(selected_run.get("sharpe_ratio")))
    metrics[4].metric("Trades", format_number(selected_run.get("trade_count"), decimals=0))

    trades = queries.load_backtest_trades(database_path, int(run_id))
    equity_curve = queries.load_backtest_equity_curve(database_path, int(run_id))
    if not equity_curve.empty:
        st.subheader("Equity curve")
        st.plotly_chart(build_equity_curve_chart(equity_curve), width="stretch")
        st.subheader("Drawdown")
        st.plotly_chart(build_drawdown_chart(equity_curve), width="stretch")
    else:
        st.info("No equity curve rows are stored for this backtest run.")

    if not trades.empty:
        pnl = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0)
        trade_metrics = st.columns(4)
        trade_metrics[0].metric("Total PnL", format_number(pnl.sum()))
        trade_metrics[1].metric("Average PnL", format_number(pnl.mean()))
        trade_metrics[2].metric("Best trade", format_number(pnl.max()))
        trade_metrics[3].metric("Worst trade", format_number(pnl.min()))
        st.plotly_chart(build_trade_pnl_chart(trades), width="stretch")

    overlay_bars = queries.load_market_bars(
        database_path,
        str(selected_run["symbol"]),
        str(selected_run["asset_class"]),
        str(selected_run["timeframe"]),
        _date_filter_from_value(selected_run.get("start")),
        _date_filter_from_value(selected_run.get("end"), include_next_day=True),
    )
    if not overlay_bars.empty:
        st.subheader("Trade overlay")
        st.plotly_chart(build_trade_overlay_chart(overlay_bars, trades), width="stretch")

    st.subheader("Trades")
    st.dataframe(trades, hide_index=True, width="stretch")

    with st.expander("Backtest stats JSON"):
        stats = parse_json(selected_run.get("stats_json"))
        st.json(stats or {})


# --- helpers ------------------------------------------------------------

_FEATURE_KEYS = [
    "sma_20",
    "sma_50",
    "rsi_14",
    "macd",
    "macd_hist",
    "macd_signal",
    "atr_14",
    "bbands_20_2_lower",
    "bbands_20_2_upper",
]


def _decision_table(decisions: pd.DataFrame) -> pd.DataFrame:
    table = _select_columns(
        decisions,
        [
            "bar_timestamp",
            "symbol",
            "timeframe",
            "strategy",
            "decision",
            "latest_close",
            "stop_loss",
            "take_profit",
            "decision_reason",
            "notification",
            "order_status",
            "order_id",
            "order_error",
        ],
    ).copy()
    if "decision" in table.columns:
        table["decision"] = table["decision"].map(lambda value: DECISION_LABELS.get(value, value))
    return table


def _decision_label(decisions: pd.DataFrame, signal_id: int) -> str:
    row = decisions[decisions["id"] == signal_id].iloc[0]
    return (
        f"#{signal_id} {DECISION_LABELS.get(row['decision'], row['decision'])} "
        f"{row['symbol']} {_date_text(row['bar_timestamp'])}"
    )


def _decision_columns(st) -> dict:
    return {
        "bar_timestamp": st.column_config.DatetimeColumn("Bar", format="YYYY-MM-DD HH:mm"),
        "decision": st.column_config.TextColumn("Alert"),
        "latest_close": st.column_config.NumberColumn("Price", format="%.4f"),
        "stop_loss": st.column_config.NumberColumn("Stop", format="%.4f"),
        "take_profit": st.column_config.NumberColumn("Target", format="%.4f"),
        "decision_reason": st.column_config.TextColumn("Why", width="large"),
    }


def _position_columns(st) -> dict:
    percent = "%+.2f%%"
    return {
        "entry_time": st.column_config.DatetimeColumn("Entry time", format="YYYY-MM-DD HH:mm"),
        "exit_time": st.column_config.DatetimeColumn("Exit time", format="YYYY-MM-DD HH:mm"),
        "last_bar_timestamp": st.column_config.DatetimeColumn("Last bar", format="YYYY-MM-DD HH:mm"),
        "entry_price": st.column_config.NumberColumn("Entry", format="%.4f"),
        "mark_price": st.column_config.NumberColumn("Last / exit", format="%.4f"),
        "pnl_pct": st.column_config.NumberColumn("P/L", format=percent),
        "to_stop_pct": st.column_config.NumberColumn("To stop", format=percent),
        "to_target_pct": st.column_config.NumberColumn("To target", format=percent),
        "stop_loss": st.column_config.NumberColumn("Stop", format="%.4f"),
        "take_profit": st.column_config.NumberColumn("Target", format="%.4f"),
    }


def _select_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[[column for column in columns if column in frame.columns]]


def _jsonable_row(row: pd.Series) -> dict[str, object]:
    output = {}
    for key, value in row.to_dict().items():
        if key in {"features", "patterns"}:
            output[key] = value
        elif pd.isna(value):
            output[key] = None
        elif hasattr(value, "isoformat"):
            output[key] = value.isoformat()
        else:
            output[key] = value
    return output


def _date_text(value: object) -> str:
    if pd.isna(value):
        return "-"
    if hasattr(value, "date"):
        return str(value.date())
    return str(value)


def _date_filter_from_value(value: object, include_next_day: bool = False) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if include_next_day:
        timestamp = timestamp + timedelta(days=1)
    return timestamp.isoformat()


def main() -> None:
    from streamlit.web import cli as streamlit_cli

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    app_path = Path(__file__).resolve()
    sys.argv = ["streamlit", "run", *sys.argv[1:], str(app_path)]
    sys.exit(streamlit_cli.main())


if __name__ == "__main__":
    render()
