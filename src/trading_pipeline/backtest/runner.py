from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_pipeline.domain import AssetClass, Bar, SignalAction
from trading_pipeline.features import compute_feature_frame, snapshot_from_frame
from trading_pipeline.features.frame import bars_to_ohlcv_frame
from trading_pipeline.strategies.sma_rsi import generate_signal_from_features


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
    feature_frame: pd.DataFrame


def run_strategy_backtest(bars: list[Bar], config: BacktestConfig) -> BacktestResult:
    try:
        from backtesting import Backtest
        from backtesting.lib import FractionalBacktest
    except ImportError as exc:
        raise RuntimeError(
            "backtesting.py is not installed. Run: pip install -e .[dev]"
        ) from exc

    feature_frame = compute_feature_frame(bars)
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

    backtest = backtest_class(feature_frame, strategy, **backtest_kwargs)
    stats = backtest.run()
    return BacktestResult(
        stats=stats,
        trades=stats["_trades"],
        equity_curve=stats["_equity_curve"],
        feature_frame=feature_frame,
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
            current_length = len(self.data)
            if current_length < 60:
                return

            visible_frame = self.data.df.iloc[:current_length]
            signal = generate_signal_from_features(
                symbol=self.symbol,
                asset_class=self.asset_class,
                latest=snapshot_from_frame(visible_frame, -1),
                previous=snapshot_from_frame(visible_frame, -2),
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
