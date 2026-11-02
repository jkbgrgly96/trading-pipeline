from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from trading_pipeline.brokers.base import Broker
from trading_pipeline.decisions import decide
from trading_pipeline.domain import AssetClass, Bar, DecisionAction, OpenPosition
from trading_pipeline.features import compute_feature_frame, snapshots_from_frame
from trading_pipeline.features.frame import bars_to_ohlcv_frame
from trading_pipeline.strategies import DEFAULT_STRATEGY, History, Strategy, get_strategy
from trading_pipeline.timeframes import closed_bars, parse_timeframe

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "bars_to_ohlcv_frame",
    "default_costs",
    "fetch_backtest_bars",
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
    # USD per entry, like a paper order. Ignored when ``position_size`` is set.
    notional: float = 100.0
    # Fraction of current equity per entry (compounding). ``None``: use ``notional``.
    position_size: float | None = None
    trade_on_close: bool = False
    fractional_unit: float = 0.00000001
    # First bar that may trade; earlier bars only warm up the indicators.
    start: datetime | None = None

    @property
    def sizing(self) -> str:
        return "notional" if self.position_size is None else "equity_fraction"

    def validate(self) -> None:
        if self.position_size is not None and not 0 < self.position_size <= 1:
            raise ValueError("position_size must be in (0, 1].")
        if self.position_size is None and not 0 < self.notional <= self.cash:
            raise ValueError(f"notional must be in (0, cash={self.cash:g}].")


@dataclass(frozen=True)
class BacktestResult:
    stats: Any
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    feature_frame: pd.DataFrame


def fetch_backtest_bars(
    broker: Broker,
    symbol: str,
    asset_class: AssetClass,
    timeframe: str,
    min_bars: int,
    limit: int,
    start: datetime | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
) -> list[Bar]:
    """Closed bars for a backtest.

    With ``start``, every bar from ``start`` to ``end`` (default: now), plus
    enough earlier bars to warm up ``min_bars`` of indicators. Without it,
    the latest ``limit`` bars.
    """
    now = now or datetime.now(UTC)
    parsed = parse_timeframe(timeframe)
    if start is None:
        bars = broker.get_bars(symbol=symbol, asset_class=asset_class, timeframe=timeframe, limit=limit)
    else:
        warmup_start = start - parsed.lookback(asset_class, min_bars)
        bars = broker.get_bars_between(symbol, asset_class, timeframe, warmup_start, end or now)
    return closed_bars(bars, parsed, now)


def run_strategy_backtest(bars: list[Bar], config: BacktestConfig) -> BacktestResult:
    from backtesting.lib import FractionalBacktest

    config.validate()
    strategy = get_strategy(config.strategy)
    feature_frame = compute_feature_frame(bars, strategy.feature_definitions())
    first_tradable = _first_tradable_index(feature_frame.index, strategy.min_bars, config.start)
    # Backtesting.py calls ``next`` from its second row on, so the data starts
    # one bar early. Warm-up bars before that would only distort Buy & Hold,
    # duration and exposure; the precomputed features already cover them.
    offset = max(first_tradable - 1, 0)
    strategy_class = _build_strategy_class(config, strategy, feature_frame, offset, first_tradable)

    # Fractional units for stocks too: Alpaca's notional orders buy fractional shares.
    backtest = FractionalBacktest(
        feature_frame.iloc[offset:],
        strategy_class,
        cash=config.cash,
        commission=config.commission,
        spread=config.spread,
        trade_on_close=config.trade_on_close,
        exclusive_orders=True,
        finalize_trades=True,
        fractional_unit=config.fractional_unit,
    )
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
        "Avg. Trade [%]",
    ]
    summary = {key: stats[key] for key in keys if key in stats}
    if "_trades" in stats:
        trades = stats["_trades"]
        summary["P/L [$]"] = float(trades["PnL"].sum()) if len(trades) else 0.0
    return summary


def _first_tradable_index(index: pd.Index, min_bars: int, start: datetime | None) -> int:
    first = min_bars - 1
    if start is not None:
        timestamps = pd.DatetimeIndex(index)
        start_ts = pd.Timestamp(start)
        if timestamps.tz is not None and start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        first = max(first, int(timestamps.searchsorted(start_ts, side="left")))
    if first >= len(index):
        raise ValueError(
            f"No bars to trade: {len(index)} bars, the first tradable one would be #{first + 1} "
            "(indicator warm-up and start date). Fetch more history."
        )
    return first


def _build_strategy_class(
    config: BacktestConfig,
    strategy: Strategy,
    feature_frame: pd.DataFrame,
    offset: int,
    first_tradable: int,
):
    from backtesting import Strategy as BacktestingStrategy

    # Features are causal (verified: values at bar i do not change when later
    # bars are added), so precomputing every snapshot once is safe and avoids
    # rebuilding a snapshot from ~75 columns on every step.
    snapshots = snapshots_from_frame(feature_frame)

    def entry_size(price: float) -> float | None:
        if config.position_size is not None:
            return config.position_size
        # FractionalBacktest trades whole multiples of ``fractional_unit``.
        units = int(config.notional / (price * config.fractional_unit))
        return units if units >= 1 else None

    class PipelineStrategy(BacktestingStrategy):
        symbol = config.symbol
        asset_class = config.asset_class

        def init(self) -> None:
            self._open: OpenPosition | None = None

        def next(self) -> None:
            index = offset + len(self.data) - 1
            if index < first_tradable:
                return

            latest = snapshots[index]
            signal = strategy.signal(self.symbol, self.asset_class, History(snapshots, index + 1))
            if not self.position:
                # Closed by the engine (e.g. finalize) or never filled.
                self._open = None

            decision = decide(signal, self._open, latest)
            if decision.action == DecisionAction.ENTER_LONG and not self.position:
                size = entry_size(latest.close)
                if size is None:
                    return
                self.buy(size=size)
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
