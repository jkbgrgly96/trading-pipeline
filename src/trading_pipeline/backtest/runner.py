from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from trading_pipeline.decisions import decide
from trading_pipeline.domain import AssetClass, Bar, DecisionAction, OpenPosition
from trading_pipeline.features import compute_feature_frame, snapshots_from_frame
from trading_pipeline.features.frame import bars_to_ohlcv_frame
from trading_pipeline.strategies import DEFAULT_STRATEGY, History, Strategy, get_strategy

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "bars_to_ohlcv_frame",
    "default_costs",
    "run_strategy_backtest",
    "summarize_stats",
]


def default_costs(asset_class: AssetClass) -> tuple[float, float]:
    """(commission, spread) defaults that are roughly realistic for Alpaca.

    Stocks are commission-free but you still pay the bid/ask spread; crypto
    has a taker fee of roughly 0.25% at the lowest volume tier.
    """
    if asset_class == AssetClass.CRYPTO:
        return 0.0025, 0.0005
    return 0.0, 0.0005


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    asset_class: AssetClass
    strategy: str = DEFAULT_STRATEGY
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
    from backtesting import Backtest
    from backtesting.lib import FractionalBacktest

    strategy = get_strategy(config.strategy)
    feature_frame = compute_feature_frame(bars, strategy.feature_definitions())
    strategy_class = _build_strategy_class(config, strategy, feature_frame)
    backtest_class = (
        FractionalBacktest if config.asset_class == AssetClass.CRYPTO else Backtest
    )
    backtest_kwargs: dict[str, Any] = {
        "cash": config.cash,
        "commission": config.commission,
        "spread": config.spread,
        "trade_on_close": config.trade_on_close,
        "exclusive_orders": True,
        "finalize_trades": True,
    }
    if backtest_class is FractionalBacktest:
        backtest_kwargs["fractional_unit"] = config.fractional_unit

    backtest = backtest_class(feature_frame, strategy_class, **backtest_kwargs)
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


def _build_strategy_class(config: BacktestConfig, strategy: Strategy, feature_frame: pd.DataFrame):
    from backtesting import Strategy as BacktestingStrategy

    # Features are causal (verified: values at bar i do not change when later
    # bars are added), so precomputing every snapshot once is safe and avoids
    # rebuilding a snapshot from ~75 columns on every step.
    snapshots = snapshots_from_frame(feature_frame)

    class PipelineStrategy(BacktestingStrategy):
        symbol = config.symbol
        asset_class = config.asset_class
        position_size = config.position_size

        def init(self) -> None:
            self._open: OpenPosition | None = None

        def next(self) -> None:
            index = len(self.data) - 1
            if index + 1 < strategy.min_bars:
                return

            latest = snapshots[index]
            signal = strategy.signal(self.symbol, self.asset_class, History(snapshots, index + 1))
            if not self.position:
                # Closed by the engine (e.g. finalize) or never opened.
                self._open = None

            decision = decide(signal, self._open, latest)
            if decision.action == DecisionAction.ENTER_LONG and not self.position:
                self.buy(size=self.position_size)
                self._open = OpenPosition(
                    symbol=self.symbol,
                    asset_class=self.asset_class,
                    timeframe="backtest",
                    entry_price=latest.close,
                    entry_time=_as_datetime(latest.timestamp),
                    stop_loss=decision.stop_loss,
                    take_profit=decision.take_profit,
                )
            elif decision.action == DecisionAction.EXIT_LONG and self.position:
                self.position.close()
                self._open = None

    PipelineStrategy.__name__ = PipelineStrategy.__qualname__ = f"Pipeline[{strategy.name}]"
    return PipelineStrategy


def _as_datetime(value: Any) -> datetime:
    return pd.Timestamp(value).to_pydatetime()
