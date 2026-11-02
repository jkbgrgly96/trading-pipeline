from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def build_market_chart(
    bars: pd.DataFrame,
    features: pd.DataFrame,
    signals: pd.DataFrame,
    show_sma: bool = True,
    show_bbands: bool = True,
    show_signals: bool = True,
    show_hold_signals: bool = False,
    show_rsi: bool = True,
    show_macd: bool = True,
    show_atr: bool = True,
    positions: pd.DataFrame | None = None,
    show_positions: bool = True,
) -> go.Figure:
    rows = _rows(show_rsi=show_rsi, show_macd=show_macd, show_atr=show_atr)
    row_heights = _row_heights(rows)
    figure = make_subplots(
        rows=len(rows),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=[label for label, _ in rows],
    )

    if bars.empty:
        figure.update_layout(template="plotly_white", height=600)
        return figure

    merged = _merge_bars_and_features(bars, features)
    _add_candles(figure, merged, row=1)
    _add_volume(figure, merged, row=2)

    if show_sma:
        _add_line(figure, merged, "sma_20", "SMA 20", row=1, color="#2563eb")
        _add_line(figure, merged, "sma_50", "SMA 50", row=1, color="#f97316")

    if show_bbands:
        _add_line(
            figure,
            merged,
            "bbands_20_2_upper",
            "BB Upper",
            row=1,
            color="#64748b",
            width=1,
        )
        _add_line(
            figure,
            merged,
            "bbands_20_2_lower",
            "BB Lower",
            row=1,
            color="#64748b",
            width=1,
        )

    if show_signals and not signals.empty:
        _add_signal_markers(
            figure,
            signals,
            show_hold_signals=show_hold_signals,
            row=1,
        )
        _add_decision_markers(figure, signals, row=1)

    if show_positions and positions is not None and not positions.empty:
        _add_position_levels(figure, positions, merged["timestamp"].max(), row=1)

    row_lookup = {key: index + 1 for index, (_, key) in enumerate(rows)}
    if show_rsi and "rsi" in row_lookup:
        _add_line(figure, merged, "rsi_14", "RSI 14", row=row_lookup["rsi"], color="#7c3aed")
        figure.add_hline(y=70, line_dash="dot", line_color="#dc2626", row=row_lookup["rsi"], col=1)
        figure.add_hline(y=30, line_dash="dot", line_color="#16a34a", row=row_lookup["rsi"], col=1)

    if show_macd and "macd" in row_lookup:
        macd_row = row_lookup["macd"]
        _add_line(figure, merged, "macd", "MACD", row=macd_row, color="#2563eb")
        _add_line(figure, merged, "macd_signal", "MACD Signal", row=macd_row, color="#f97316")
        if "macd_hist" in merged.columns:
            figure.add_trace(
                go.Bar(
                    x=merged["timestamp"],
                    y=merged["macd_hist"],
                    name="MACD Hist",
                    marker_color="#94a3b8",
                    opacity=0.7,
                ),
                row=macd_row,
                col=1,
            )

    if show_atr and "atr" in row_lookup:
        _add_line(figure, merged, "atr_14", "ATR 14", row=row_lookup["atr"], color="#0f766e")

    figure.update_layout(
        template="plotly_white",
        height=max(720, 180 * len(rows)),
        margin={"l": 40, "r": 20, "t": 60, "b": 30},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )
    figure.update_yaxes(title_text="Price", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    return figure


def build_trade_pnl_chart(trades: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if trades.empty or "pnl" not in trades.columns:
        figure.update_layout(template="plotly_white", height=260)
        return figure

    pnl = pd.to_numeric(trades["pnl"], errors="coerce").fillna(0)
    x_values = trades["exit_time"] if "exit_time" in trades.columns else trades.index
    colors = ["#16a34a" if value >= 0 else "#dc2626" for value in pnl]
    figure.add_trace(
        go.Bar(
            x=x_values,
            y=pnl,
            name="PnL",
            marker_color=colors,
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=280,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        yaxis_title="PnL",
        hovermode="x unified",
    )
    return figure


def build_equity_curve_chart(equity_curve: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if equity_curve.empty or "equity" not in equity_curve.columns:
        figure.update_layout(template="plotly_white", height=320)
        return figure

    figure.add_trace(
        go.Scatter(
            x=equity_curve["timestamp"],
            y=equity_curve["equity"],
            mode="lines",
            name="Equity",
            line={"color": "#2563eb", "width": 2},
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=340,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        yaxis_title="Equity",
        hovermode="x unified",
    )
    return figure


def build_drawdown_chart(equity_curve: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if equity_curve.empty or "drawdown_pct" not in equity_curve.columns:
        figure.update_layout(template="plotly_white", height=260)
        return figure

    figure.add_trace(
        go.Scatter(
            x=equity_curve["timestamp"],
            y=equity_curve["drawdown_pct"],
            mode="lines",
            name="Drawdown",
            fill="tozeroy",
            line={"color": "#dc2626", "width": 1.5},
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=280,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        yaxis_title="Drawdown %",
        hovermode="x unified",
    )
    return figure


def build_trade_overlay_chart(
    bars: pd.DataFrame,
    trades: pd.DataFrame,
) -> go.Figure:
    figure = go.Figure()
    if bars.empty:
        figure.update_layout(template="plotly_white", height=420)
        return figure

    figure.add_trace(
        go.Candlestick(
            x=bars["timestamp"],
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            name="OHLC",
            increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626",
        )
    )

    if not trades.empty:
        _add_trade_points(figure, trades, "entry_time", "entry_price", "Entry", "triangle-up")
        _add_trade_points(figure, trades, "exit_time", "exit_price", "Exit", "triangle-down")

    figure.update_layout(
        template="plotly_white",
        height=520,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        yaxis_title="Price",
    )
    return figure


def _rows(show_rsi: bool, show_macd: bool, show_atr: bool) -> list[tuple[str, str]]:
    rows = [("Price", "price"), ("Volume", "volume")]
    if show_rsi:
        rows.append(("RSI", "rsi"))
    if show_macd:
        rows.append(("MACD", "macd"))
    if show_atr:
        rows.append(("ATR", "atr"))
    return rows


def _row_heights(rows: list[tuple[str, str]]) -> list[float]:
    weights = []
    for _, key in rows:
        if key == "price":
            weights.append(0.52)
        elif key == "volume":
            weights.append(0.14)
        else:
            weights.append(0.17)
    total = sum(weights)
    return [weight / total for weight in weights]


def _merge_bars_and_features(bars: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return bars.copy()
    feature_columns = [
        column
        for column in features.columns
        if column
        not in {
            "id",
            "symbol",
            "asset_class",
            "timeframe",
            "features_json",
            "patterns_json",
            "features",
            "patterns",
            "created_at",
        }
    ]
    return bars.merge(
        features[feature_columns],
        how="left",
        on="timestamp",
        suffixes=("", "_feature"),
    )


def _add_candles(figure: go.Figure, frame: pd.DataFrame, row: int) -> None:
    figure.add_trace(
        go.Candlestick(
            x=frame["timestamp"],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="OHLC",
            increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626",
        ),
        row=row,
        col=1,
    )


def _add_volume(figure: go.Figure, frame: pd.DataFrame, row: int) -> None:
    colors = [
        "#16a34a" if close >= open_ else "#dc2626"
        for open_, close in zip(frame["open"], frame["close"], strict=True)
    ]
    figure.add_trace(
        go.Bar(
            x=frame["timestamp"],
            y=frame["volume"],
            name="Volume",
            marker_color=colors,
            opacity=0.45,
        ),
        row=row,
        col=1,
    )


def _add_line(
    figure: go.Figure,
    frame: pd.DataFrame,
    column: str,
    name: str,
    row: int,
    color: str,
    width: int = 2,
) -> None:
    if column not in frame.columns:
        return
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame[column],
            mode="lines",
            name=name,
            line={"color": color, "width": width},
        ),
        row=row,
        col=1,
    )


def _add_signal_markers(
    figure: go.Figure,
    signals: pd.DataFrame,
    show_hold_signals: bool,
    row: int,
) -> None:
    # Raw strategy signals are drawn small and faded; the decisions you were
    # alerted about are drawn on top by _add_decision_markers.
    markers = {
        "buy": {"symbol": "triangle-up-open", "color": "#16a34a", "size": 9},
        "sell": {"symbol": "triangle-down-open", "color": "#dc2626", "size": 9},
        "hold": {"symbol": "circle", "color": "#64748b", "size": 6},
    }
    for action, marker in markers.items():
        if action == "hold" and not show_hold_signals:
            continue
        subset = signals[signals["action"] == action]
        if subset.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=subset["bar_timestamp"],
                y=subset["latest_close"],
                mode="markers",
                name=f"{action.upper()} signal",
                text=subset.get("reason"),
                marker={
                    "symbol": marker["symbol"],
                    "color": marker["color"],
                    "size": marker["size"],
                    "line": {"color": "#ffffff", "width": 1},
                },
            ),
            row=row,
            col=1,
        )


def _add_trade_points(
    figure: go.Figure,
    trades: pd.DataFrame,
    time_column: str,
    price_column: str,
    name: str,
    marker_symbol: str,
) -> None:
    if time_column not in trades.columns or price_column not in trades.columns:
        return
    pnl = pd.to_numeric(trades.get("pnl"), errors="coerce").fillna(0)
    colors = ["#16a34a" if value >= 0 else "#dc2626" for value in pnl]
    figure.add_trace(
        go.Scatter(
            x=trades[time_column],
            y=trades[price_column],
            mode="markers",
            name=name,
            text=trades.get("pnl"),
            marker={
                "symbol": marker_symbol,
                "color": colors,
                "size": 11,
                "line": {"color": "#ffffff", "width": 1},
            },
        )
    )


DECISION_MARKERS = {
    "enter_long": {"name": "ENTER (alert)", "symbol": "triangle-up", "color": "#15803d"},
    "exit_long": {"name": "EXIT (alert)", "symbol": "x", "color": "#b91c1c"},
}


def _add_decision_markers(figure: go.Figure, signals: pd.DataFrame, row: int) -> None:
    if "decision" not in signals.columns:
        return
    for decision, style in DECISION_MARKERS.items():
        subset = signals[signals["decision"] == decision]
        if subset.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=subset["bar_timestamp"],
                y=subset["latest_close"],
                mode="markers",
                name=style["name"],
                text=subset.get("decision_reason"),
                hovertemplate="%{text}<extra>" + style["name"] + "</extra>",
                marker={
                    "symbol": style["symbol"],
                    "color": style["color"],
                    "size": 16,
                    "line": {"color": "#ffffff", "width": 1.5},
                },
            ),
            row=row,
            col=1,
        )


def _add_position_levels(
    figure: go.Figure,
    positions: pd.DataFrame,
    last_timestamp: pd.Timestamp,
    row: int,
) -> None:
    """Entry, stop and target as segments spanning each position's lifetime."""
    levels = [
        ("entry_price", "Entry", "#2563eb", "solid"),
        ("stop_loss", "Stop", "#dc2626", "dash"),
        ("take_profit", "Target", "#16a34a", "dash"),
    ]
    for column, label, color, dash in levels:
        xs: list = []
        ys: list = []
        for _, position in positions.iterrows():
            level = position.get(column)
            if level is None or pd.isna(level):
                continue
            end = position.get("exit_time")
            if end is None or pd.isna(end):
                end = last_timestamp
            xs += [position["entry_time"], end, None]
            ys += [level, level, None]
        if not xs:
            continue
        figure.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines",
                name=label,
                line={"color": color, "width": 1.5, "dash": dash},
                connectgaps=False,
            ),
            row=row,
            col=1,
        )


# Diverging P/L scale: loss (red) <- neutral gray -> profit (blue).
LOSS_COLOR = "#e34948"
NEUTRAL_COLOR = "#f0efec"
PROFIT_COLOR = "#2a78d6"


def build_pnl_heatmap(
    frame: pd.DataFrame,
    row_column: str,
    rows: list[str],
    periods: list[str],
    check_periods: set[str] | None = None,
) -> go.Figure:
    """P/L per row (configuration or symbol) and period, on a diverging scale.

    ``frame`` has one row per (``row_column``, period) with pnl, trades,
    win_rate_pct and profit_factor. Cells without data stay empty.
    """
    check_periods = check_periods or set()
    columns = [f"{p} (check)" if p in check_periods else p for p in periods]
    figure = go.Figure()
    if frame.empty or not rows:
        figure.update_layout(template="plotly_white", height=200)
        return figure

    lookup = frame.set_index([row_column, "period"])
    z, text, hover = [], [], []
    for row in rows:
        z_row, text_row, hover_row = [], [], []
        for period in periods:
            if (row, period) in lookup.index and lookup.loc[(row, period), "trades"] > 0:
                cell = lookup.loc[(row, period)]
                pf = cell["profit_factor"]
                pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
                z_row.append(float(cell["pnl"]))
                text_row.append(f"${cell['pnl']:,.0f}")
                hover_row.append(
                    f"{row} · {period}<br>P/L ${cell['pnl']:,.2f} · {int(cell['trades'])} trades"
                    f"<br>win {cell['win_rate_pct']:.0f}% · PF {pf_text}"
                )
            else:
                z_row.append(None)
                text_row.append("no trades")
                hover_row.append(f"{row} · {period}<br>no trades or no data")
        z.append(z_row)
        text.append(text_row)
        hover.append(hover_row)

    limit = max((abs(value) for row in z for value in row if value is not None), default=1.0) or 1.0
    figure.add_trace(
        go.Heatmap(
            z=z,
            x=columns,
            y=rows,
            text=text,
            texttemplate="%{text}",
            textfont={"color": "#0b0b0b", "size": 12},
            customdata=hover,
            hovertemplate="%{customdata}<extra></extra>",
            colorscale=[[0.0, LOSS_COLOR], [0.5, NEUTRAL_COLOR], [1.0, PROFIT_COLOR]],
            zmin=-limit,
            zmax=limit,
            zmid=0,
            xgap=2,
            ygap=2,
            colorbar={"title": {"text": "P/L $"}, "thickness": 12},
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=max(220, 34 * len(rows) + 90),
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        xaxis={"side": "top", "showgrid": False},
        yaxis={"autorange": "reversed", "showgrid": False},
    )
    return figure
