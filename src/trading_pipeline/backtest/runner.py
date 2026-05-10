from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_pipeline.domain import AssetClass, Bar, SignalAction
from trading_pipeline.strategies.sma_rsi import generate_signal


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    asset_class: AssetClass
    cash: float = 10_000.0
    commission: float = 0.0
    spread: float = 0.0
    position_size: float = 0.95
    trade_on_close: bool = False
    fractional_unit: float = 0.00000001


@dataclass(frozen=True)
class BacktestResult:
    stats: Any
    trades: pd.DataFrame
    equity_curve: pd.DataFrame


def bars_to_ohlcv_frame(bars: list[Bar]) -> pd.DataFrame:
    if not bars:
        raise ValueError("Cannot backtest an empty bar list.")

    frame = pd.DataFrame(
        {
            "Open": [bar.open for bar in bars],
            "High": [bar.high for bar in bars],
            "Low": [bar.low for bar in bars],
            "Close": [bar.close for bar in bars],
            "Volume": [bar.volume for bar in bars],
        },
        index=pd.DatetimeIndex([bar.timestamp for bar in bars], name="Timestamp"),
    )
    return frame.sort_index()


def run_strategy_backtest(bars: list[Bar], config: BacktestConfig) -> BacktestResult:
    try:
        from backtesting import Backtest
        from backtesting.lib import FractionalBacktest
    except ImportError as exc:
        raise RuntimeError(
            "backtesting.py is not installed. Run: pip install -e .[dev]"
        ) from exc

    strategy = _build_strategy_class(config)
    backtest_class = (
        FractionalBacktest if config.asset_class == AssetClass.CRYPTO else Backtest
    )
    backtest_kwargs = {
        "cash": config.cash,
        "commission": config.commission,
        "spread": config.spread,
        "trade_on_close": config.trade_on_close,
        "exclusive_orders": True,
        "finalize_trades": True,
    }
    if backtest_class is FractionalBacktest:
        backtest_kwargs["fractional_unit"] = config.fractional_unit

    backtest = backtest_class(bars_to_ohlcv_frame(bars), strategy, **backtest_kwargs)
    stats = backtest.run()
    return BacktestResult(
        stats=stats,
        trades=stats["_trades"],
        equity_curve=stats["_equity_curve"],
    )


def summarize_stats(stats: Any) -> dict[str, Any]:
    keys = [
        "Start",
        "End",
        "Duration",
        "Exposure Time [%]",
        "Equity Final [$]",
        "Return [%]",
        "Buy & Hold Return [%]",
        "Sharpe Ratio",
        "Max. Drawdown [%]",
        "# Trades",
        "Win Rate [%]",
    ]
    return {key: stats[key] for key in keys if key in stats}


def _build_strategy_class(config: BacktestConfig):
    from backtesting import Strategy

    class SmaRsiBacktestingStrategy(Strategy):
        symbol = config.symbol
        asset_class = config.asset_class
        position_size = config.position_size

        def init(self) -> None:
            pass

        def next(self) -> None:
            current_bars = _frame_to_bars(self.data.df)
            if len(current_bars) < 60:
                return

            signal = generate_signal(
                symbol=self.symbol,
                asset_class=self.asset_class,
                bars=current_bars,
            )

            if signal.action == SignalAction.BUY and not self.position:
                self.buy(
                    size=self.position_size,
                    sl=signal.stop_loss,
                    tp=signal.take_profit,
                )
            elif signal.action == SignalAction.SELL and self.position:
                self.position.close()

    return SmaRsiBacktestingStrategy


def _frame_to_bars(frame: pd.DataFrame) -> list[Bar]:
    bars: list[Bar] = []
    for timestamp, row in frame.iterrows():
        bars.append(
            Bar(
                timestamp=timestamp.to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0.0)),
            )
        )
    return bars
