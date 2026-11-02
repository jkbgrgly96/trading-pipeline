"""Parameter sweep: tune on one period, check the winners on a later one.

Every parameter combination is backtested on every watchlist entry over the
in-sample period (``start`` .. ``split``) and ranked by total P/L across
entries (risk-based sizing makes the dollars comparable). Only the best few,
the strategy's defaults and the ``sma_rsi`` baseline are then run on the
out-of-sample period (``split`` .. ``end``), which played no part in choosing
them. A configuration that only shines in-sample is overfitted.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from trading_pipeline.backtest.runner import BacktestConfig, default_costs, run_strategy_backtest
from trading_pipeline.domain import Bar
from trading_pipeline.strategies import DEFAULT_STRATEGY, STRATEGIES, coerce_params, get_strategy

logger = logging.getLogger(__name__)

# Used when no --grid is given. Kept small: every value multiplies the runs.
DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "indicathor": {
        "score_ema": [3, 5, 10],
        "entry_score": [1.0, 2.0],
        "adx_min": [0.0, 20.0],
        "macd_flag_mode": ["state", "cross"],
        "trend_filter": ["close", "cross"],
    },
    "rsi2_pullback": {
        "entry_rsi": [5.0, 10.0, 20.0],
        "exit_sma_length": [5, 10],
        "stop_atr_mult": [0.0, 3.0],
        "max_hold_bars": [0, 10],
    },
    "donchian_breakout": {
        "entry_length": [20, 55, 100],
        "exit_length": [10, 20],
        "stop_atr_mult": [2.0, 3.0],
    },
    "sma_rsi": {
        "fast": [10, 20],
        "slow": [50, 100],
    },
}


@dataclass(frozen=True)
class SweepTarget:
    """One watchlist entry with its bars (warm-up included) for the whole period."""

    symbol: str
    asset_class: Any
    timeframe: str
    bars: list[Bar]
    benchmark_bars: list[Bar] | None = None
    notional: float = 100.0
    risk_per_trade: float | None = 10.0
    max_notional: float = 1_000.0


@dataclass
class Summary:
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    trade_return_sum: float = 0.0
    buy_hold: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, result: Any) -> None:
        trades = result.trades
        self.trades += len(trades)
        if len(trades):
            pnl = trades["PnL"]
            self.wins += int((pnl > 0).sum())
            self.pnl += float(pnl.sum())
            self.gross_profit += float(pnl[pnl > 0].sum())
            self.gross_loss += float(-pnl[pnl < 0].sum())
            self.trade_return_sum += float(trades["ReturnPct"].sum()) * 100
        buy_hold = result.stats.get("Buy & Hold Return [%]")
        if buy_hold == buy_hold and buy_hold is not None:
            self.buy_hold.append(float(buy_hold))

    @property
    def win_rate(self) -> float | None:
        return 100 * self.wins / self.trades if self.trades else None

    @property
    def avg_trade_pct(self) -> float | None:
        return self.trade_return_sum / self.trades if self.trades else None

    @property
    def profit_factor(self) -> float | None:
        return self.gross_profit / self.gross_loss if self.gross_loss else None

    @property
    def avg_buy_hold_pct(self) -> float | None:
        return sum(self.buy_hold) / len(self.buy_hold) if self.buy_hold else None


@dataclass
class SweepRow:
    label: str  # "#1", "defaults", "sma_rsi baseline"
    strategy: str
    params: dict[str, Any]
    in_sample: Summary
    out_of_sample: Summary | None = None


def parse_grid(strategy: str, assignments: Sequence[str] | None) -> dict[str, list[Any]]:
    """``["adx_min=0,20", "macd_flag_mode=state,cross"]`` -> typed value lists."""
    if not assignments:
        return dict(DEFAULT_GRIDS.get(strategy, {}))
    strategy_class = STRATEGIES[strategy]
    grid: dict[str, list[Any]] = {}
    for assignment in assignments:
        key, sep, raw = assignment.partition("=")
        values = [value.strip() for value in raw.split(",") if value.strip()]
        if not sep or not key.strip() or not values:
            raise ValueError(f"--grid must look like key=v1,v2, got {assignment!r}.")
        key = key.strip()
        grid[key] = [coerce_params(strategy_class, strategy, {key: value})[key] for value in values]
    return grid


def combinations(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[k] for k in keys))]


def valid_combinations(
    strategy: str, base: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]
) -> list[dict[str, Any]]:
    """Grid combinations (on top of ``base``) the strategy accepts; others are skipped."""
    valid = []
    for combo in combinations(grid):
        params = {**base, **combo}
        try:
            get_strategy(strategy, params)
        except ValueError as exc:
            logger.info("Skipping %s %s: %s", strategy, params, exc)
            continue
        valid.append(params)
    return valid


def run_sweep(
    targets: Sequence[SweepTarget],
    strategy: str,
    grid: Mapping[str, Sequence[Any]],
    start: datetime,
    split: datetime,
    end: datetime | None = None,
    base_params: Mapping[str, Any] | None = None,
    cash: float = 10_000.0,
    top: int = 5,
    min_trades: int = 30,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[SweepRow], list[SweepRow]]:
    """Returns (every in-sample row ranked best first, the out-of-sample rows)."""
    base = dict(base_params or {})
    candidates = valid_combinations(strategy, base, grid) or [base]
    in_sample_rows = []
    for number, params in enumerate(candidates, 1):
        if progress:
            progress(f"in-sample {number}/{len(candidates)}: {params}")
        summary = _evaluate(targets, strategy, params, start, split, end, cash, out_of_sample=False)
        in_sample_rows.append(SweepRow("", strategy, params, summary))

    eligible = [row for row in in_sample_rows if row.in_sample.trades >= min_trades]
    ranked = sorted(eligible, key=lambda row: row.in_sample.pnl, reverse=True)
    ranked += sorted(
        (row for row in in_sample_rows if row.in_sample.trades < min_trades),
        key=lambda row: row.in_sample.pnl,
        reverse=True,
    )
    for position, row in enumerate(ranked, 1):
        row.label = f"#{position}" if row.in_sample.trades >= min_trades else f"#{position} (few trades)"

    finalists = [row for row in ranked if row.in_sample.trades >= min_trades][:top]
    defaults = SweepRow("defaults", strategy, dict(base), _evaluate(
        targets, strategy, base, start, split, end, cash, out_of_sample=False))
    baseline = SweepRow("sma_rsi baseline", DEFAULT_STRATEGY, {}, _evaluate(
        targets, DEFAULT_STRATEGY, {}, start, split, end, cash, out_of_sample=False))
    out_rows = []
    for row in [*finalists, defaults, baseline]:
        if progress:
            progress(f"out-of-sample: {row.label} {row.params}")
        out = SweepRow(row.label, row.strategy, row.params, row.in_sample)
        out.out_of_sample = _evaluate(targets, row.strategy, row.params, start, split, end, cash, out_of_sample=True)
        out_rows.append(out)
    return ranked, out_rows


def _evaluate(
    targets: Sequence[SweepTarget],
    strategy: str,
    params: Mapping[str, Any],
    start: datetime,
    split: datetime,
    end: datetime | None,
    cash: float,
    out_of_sample: bool,
) -> Summary:
    summary = Summary()
    for target in targets:
        bars = [bar for bar in target.bars if (end is None or bar.timestamp < end)]
        if not out_of_sample:
            bars = [bar for bar in bars if bar.timestamp < split]
        commission, spread = default_costs(target.asset_class)
        config = BacktestConfig(
            symbol=target.symbol,
            asset_class=target.asset_class,
            strategy=strategy,
            strategy_params=dict(params),
            cash=cash,
            commission=commission,
            spread=spread,
            notional=target.notional,
            risk_per_trade=target.risk_per_trade,
            max_notional=target.max_notional,
            start=split if out_of_sample else start,
        )
        try:
            result = run_strategy_backtest(bars, config, target.benchmark_bars, chart_features=False)
        except ValueError as exc:
            summary.errors.append(f"{target.symbol}: {exc}")
            continue
        summary.add(result)
    return summary
