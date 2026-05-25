from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from streamlit.web import cli as streamlit_cli

import pandas as pd

from trading_pipeline.config import load_settings
from trading_pipeline.dashboard.charts import build_market_chart, build_trade_pnl_chart
from trading_pipeline.dashboard import queries
from trading_pipeline.dashboard.formatting import (
    compact_dict,
    format_number,
    format_percent,
    parse_json,
)


def render() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise RuntimeError(
            "Streamlit is not installed. Run: pip install -e .[dev]"
        ) from exc

    st.set_page_config(page_title="Trading Pipeline", layout="wide")
    st.title("Trading Pipeline Dashboard")

    settings = load_settings()
    with st.sidebar:
        st.header("Filters")
        database_path = st.text_input("Database path", settings.database_path)
        db_path = Path(database_path)
        if st.button("Refresh"):
            st.rerun()

    if not db_path.exists():
        st.warning(f"Database not found: {db_path}")
        st.stop()

    symbols = queries.load_symbols(db_path)
    if symbols.empty:
        st.info("No stored market data, signals, or backtests found.")
        st.stop()

    selected = _sidebar_filters(st, symbols)
    start_iso = selected["start"].isoformat() if selected["start"] else None
    end_iso = (selected["end"] + timedelta(days=1)).isoformat() if selected["end"] else None

    overview_tab, signals_tab, chart_tab, backtests_tab = st.tabs(
        ["Overview", "Signals", "Chart", "Backtests"]
    )

    with overview_tab:
        _render_overview(st, db_path)

    with signals_tab:
        _render_signals(
            st,
            db_path,
            selected["symbol"],
            selected["asset_class"],
            selected["timeframe"],
            selected["action"],
            start_iso,
            end_iso,
        )

    with chart_tab:
        _render_chart(
            st,
            db_path,
            selected["symbol"],
            selected["asset_class"],
            selected["timeframe"],
            start_iso,
            end_iso,
        )

    with backtests_tab:
        _render_backtests(
            st,
            db_path,
            selected["symbol"],
            selected["asset_class"],
            selected["timeframe"],
            start_iso,
            end_iso,
        )


def _sidebar_filters(st, symbols: pd.DataFrame) -> dict[str, object]:
    with st.sidebar:
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

        action = st.selectbox("Action", ["all", "buy", "sell", "hold"])

    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "timeframe": timeframe,
        "start": start,
        "end": end,
        "action": None if action == "all" else action,
    }


def _render_overview(st, database_path: Path) -> None:
    overview = queries.load_overview(database_path)
    counts = overview["table_counts"]

    columns = st.columns(5)
    columns[0].metric("Market bars", counts.get("market_bars", 0))
    columns[1].metric("Feature snapshots", counts.get("feature_snapshots", 0))
    columns[2].metric("Signals", counts.get("signals", 0))
    columns[3].metric("Backtests", counts.get("backtest_runs", 0))
    columns[4].metric("Trades", counts.get("backtest_trades", 0))

    latest_signal = overview["latest_signal"]
    latest_backtest = overview["latest_backtest"]
    last_market_bar = overview["last_market_bar"]

    st.subheader("Latest State")
    state_columns = st.columns(3)
    with state_columns[0]:
        st.caption("Latest signal")
        if latest_signal:
            st.metric("Action", latest_signal["action"].upper())
            st.write(
                {
                    "symbol": latest_signal["symbol"],
                    "timeframe": latest_signal["timeframe"],
                    "bar": latest_signal["bar_timestamp"],
                    "close": latest_signal["latest_close"],
                }
            )
        else:
            st.write("No signals")

    with state_columns[1]:
        st.caption("Latest backtest")
        if latest_backtest:
            st.metric("Return", format_percent(latest_backtest["return_pct"]))
            st.write(
                {
                    "symbol": latest_backtest["symbol"],
                    "timeframe": latest_backtest["timeframe"],
                    "trades": latest_backtest["trade_count"],
                    "max_drawdown": format_percent(latest_backtest["max_drawdown_pct"]),
                }
            )
        else:
            st.write("No backtests")

    with state_columns[2]:
        st.caption("Latest market bar")
        if last_market_bar:
            st.metric("Close", format_number(last_market_bar["close"]))
            st.write(
                {
                    "symbol": last_market_bar["symbol"],
                    "timeframe": last_market_bar["timeframe"],
                    "timestamp": last_market_bar["timestamp"],
                }
            )
        else:
            st.write("No market bars")

    action_counts = overview["signal_action_counts"]
    if not action_counts.empty:
        st.subheader("Signals By Action")
        st.bar_chart(action_counts.set_index("action")["count"])

    recent = queries.load_signals(database_path).head(10)
    if not recent.empty:
        st.subheader("Recent Signals")
        st.dataframe(
            _select_columns(
                recent,
                [
                    "bar_timestamp",
                    "symbol",
                    "asset_class",
                    "timeframe",
                    "action",
                    "confidence",
                    "latest_close",
                    "reason",
                    "patterns_text",
                ],
            ),
            use_container_width=True,
        )


def _render_signals(
    st,
    database_path: Path,
    symbol: str,
    asset_class: str,
    timeframe: str,
    action: str | None,
    start: str | None,
    end: str | None,
) -> None:
    actionable_only = st.checkbox("Show only BUY/SELL", value=False)
    signals = queries.load_signals(
        database_path,
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
        action=action,
        start=start,
        end=end,
    )
    if actionable_only and not signals.empty:
        signals = signals[signals["action"].isin(["buy", "sell"])]

    st.metric("Matching signals", len(signals))
    display_columns = [
        "created_at",
        "bar_timestamp",
        "symbol",
        "asset_class",
        "timeframe",
        "action",
        "confidence",
        "latest_close",
        "reason",
        "patterns_text",
        "rsi_14",
        "sma_20",
        "sma_50",
        "macd_hist",
        "atr_14",
        "order_id",
    ]
    st.dataframe(_select_columns(signals, display_columns), use_container_width=True)
    if not signals.empty:
        selected_signal_id = st.selectbox("Signal details", signals["id"].tolist())
        selected_signal = signals[signals["id"] == selected_signal_id].iloc[0]
        detail_columns = st.columns(2)
        with detail_columns[0]:
            st.subheader("Reason")
            st.write(selected_signal["reason"])
            st.subheader("Patterns")
            st.write(selected_signal.get("patterns_text") or "-")
        with detail_columns[1]:
            st.subheader("Feature Snapshot")
            features = selected_signal.get("features")
            st.json(
                compact_dict(
                    features if isinstance(features, dict) else None,
                    [
                        "sma_20",
                        "sma_50",
                        "rsi_14",
                        "macd",
                        "macd_hist",
                        "macd_signal",
                        "atr_14",
                        "bbands_20_2_lower",
                        "bbands_20_2_upper",
                    ],
                )
            )
        with st.expander("Raw signal row"):
            st.json(_jsonable_row(selected_signal))

    st.download_button(
        "Download signals CSV",
        data=signals.to_csv(index=False),
        file_name="signals.csv",
        mime="text/csv",
        disabled=signals.empty,
    )


def _render_chart(
    st,
    database_path: Path,
    symbol: str,
    asset_class: str,
    timeframe: str,
    start: str | None,
    end: str | None,
) -> None:
    options = st.columns(7)
    show_sma = options[0].checkbox("SMA", value=True)
    show_bbands = options[1].checkbox("Bollinger", value=True)
    show_signals = options[2].checkbox("Signals", value=True)
    show_hold = options[3].checkbox("HOLD", value=False)
    show_rsi = options[4].checkbox("RSI", value=True)
    show_macd = options[5].checkbox("MACD", value=True)
    show_atr = options[6].checkbox("ATR", value=True)

    bars = queries.load_market_bars(database_path, symbol, asset_class, timeframe, start, end)
    features = queries.load_feature_snapshots(database_path, symbol, asset_class, timeframe, start, end)
    signals = queries.load_signals(database_path, symbol, asset_class, timeframe, start=start, end=end)

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
    )
    st.plotly_chart(figure, use_container_width=True)

    summary_columns = st.columns(4)
    summary_columns[0].metric("Bars", len(bars))
    summary_columns[1].metric("Signals", len(signals))
    summary_columns[2].metric("First bar", _date_text(bars["timestamp"].min()))
    summary_columns[3].metric("Last bar", _date_text(bars["timestamp"].max()))

    if not features.empty:
        latest = features.iloc[-1]
        st.subheader("Latest Feature Snapshot")
        metric_columns = st.columns(6)
        metric_columns[0].metric("Close", format_number(bars.iloc[-1]["close"]))
        metric_columns[1].metric("RSI 14", format_number(latest.get("rsi_14")))
        metric_columns[2].metric("SMA 20", format_number(latest.get("sma_20")))
        metric_columns[3].metric("SMA 50", format_number(latest.get("sma_50")))
        metric_columns[4].metric("MACD Hist", format_number(latest.get("macd_hist")))
        metric_columns[5].metric("ATR 14", format_number(latest.get("atr_14")))
        st.caption(latest.get("patterns_text") or "No candle pattern on latest bar")


def _render_backtests(
    st,
    database_path: Path,
    symbol: str,
    asset_class: str,
    timeframe: str,
    start: str | None,
    end: str | None,
) -> None:
    runs = queries.load_backtest_runs(
        database_path,
        symbol=symbol,
        asset_class=asset_class,
        timeframe=timeframe,
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
        "bar_limit",
        "return_pct",
        "buy_hold_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "trade_count",
        "win_rate_pct",
    ]
    st.dataframe(_select_columns(runs, display_columns), use_container_width=True)

    run_id = st.selectbox("Backtest run", runs["id"].tolist())
    selected_run = runs[runs["id"] == run_id].iloc[0]
    metrics = st.columns(5)
    metrics[0].metric("Return", format_percent(selected_run.get("return_pct")))
    metrics[1].metric("Buy & Hold", format_percent(selected_run.get("buy_hold_return_pct")))
    metrics[2].metric("Max DD", format_percent(selected_run.get("max_drawdown_pct")))
    metrics[3].metric("Sharpe", format_number(selected_run.get("sharpe_ratio")))
    metrics[4].metric("Trades", format_number(selected_run.get("trade_count"), decimals=0))

    trades = queries.load_backtest_trades(database_path, int(run_id))
    if not trades.empty:
        pnl = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0)
        trade_metrics = st.columns(4)
        trade_metrics[0].metric("Total PnL", format_number(pnl.sum()))
        trade_metrics[1].metric("Average PnL", format_number(pnl.mean()))
        trade_metrics[2].metric("Best trade", format_number(pnl.max()))
        trade_metrics[3].metric("Worst trade", format_number(pnl.min()))
        st.plotly_chart(build_trade_pnl_chart(trades), use_container_width=True)

    st.subheader("Trades")
    st.dataframe(trades, use_container_width=True)

    with st.expander("Backtest stats JSON"):
        stats = parse_json(selected_run.get("stats_json"))
        st.json(stats or {})


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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    app_path = Path(__file__).resolve()
    sys.argv = ["streamlit", "run", *sys.argv[1:], str(app_path)]
    streamlit_cli.main()


if __name__ == "__main__":
    render()
