"""Parameter sweep: tune on some periods, check the winners on later ones.

Every parameter combination is backtested on every watchlist entry in every
*tune* period. Configurations are ranked by a metric over the tune periods
together (profit factor by default), preferring those that made money in every
tune period and requiring a minimum number of trades. Only the best few, the
strategy's defaults and the ``sma_rsi`` baseline are then run on the *check*
periods, which played no part in choosing them: a configuration that only
shines in the tune periods is overfitted.

Results are kept per configuration x period x entry, so the dashboard can show
which periods and symbols carry a strategy.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trading_pipeline.backtest.runner import BacktestConfig, default_costs, run_strategy_backtest
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.strategies import DEFAULT_STRATEGY, STRATEGIES, coerce_params, get_strategy
from trading_pipeline.timeframes import parse_timeframe

logger = logging.getLogger(__name__)

TUNE, CHECK = "tune", "check"
RANK_METRICS = ("profit_factor", "pnl", "expectancy")

# Used when no --grid is given. Every value multiplies the runs.
DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "indicathor": {
        "entry_score": [1.0, 1.5, 2.0, 2.5],
        "score_ema": [3, 5, 10],
        "adx_min": [0.0, 20.0, 25.0],
        "trend_filter": ["close", "cross"],
        "trail_atr_mult": [3.0, 4.0],
    },
    "rsi2_pullback": {
        "entry_rsi": [5.0, 10.0, 20.0],
        "exit_sma_length": [5, 10],
        "stop_atr_mult": [0.0, 3.0],
        "max_hold_bars": [0, 10],
    },
    "donchian_breakout": {
        "entry_length": [20, 40, 55, 80, 100],
        "exit_length": [10, 15, 20, 30],
        "stop_atr_mult": [1.5, 2.0, 3.0],
        "stop_trigger": ["low", "close"],
    },
    "sma_rsi": {
        "fast": [10, 20],
        "slow": [50, 100],
    },
}


@dataclass(frozen=True)
class Period:
    name: str
    start: datetime
    end: datetime | None  # exclusive; None: up to the latest bar
    role: str  # TUNE | CHECK

    @classmethod
    def of(cls, start: datetime, end: datetime | None, role: str) -> Period:
        last = f"{(end - timedelta(days=1)).year}" if end else "now"
        name = f"{start.year}" if last == f"{start.year}" else f"{start.year}-{last}"
        return cls(name, start, end, role)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "start": self.start, "end": self.end, "role": self.role}


@dataclass(frozen=True)
class SweepTarget:
    """One watchlist entry with its bars (warm-up included) for every period."""

    symbol: str
    asset_class: AssetClass
    timeframe: str
    bars: list[Bar]
    benchmark_bars: list[Bar] | None = None
    notional: float = 100.0
    risk_per_trade: float | None = 10.0
    max_notional: float = 1_000.0

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.timeframe}"


@dataclass
class Stats:
    """Backtest results of one entry, or several added together."""

    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    return_pct_sum: float = 0.0
    buy_hold_pct: list[float] = field(default_factory=list)
    max_drawdown_pct: list[float] = field(default_factory=list)
    exposure_pct: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_result(cls, result: Any) -> Stats:
        stats = cls()
        trades = result.trades
        stats.trades = len(trades)
        if len(trades):
            pnl = trades["PnL"]
            stats.wins = int((pnl > 0).sum())
            stats.pnl = float(pnl.sum())
            stats.gross_profit = float(pnl[pnl > 0].sum())
            stats.gross_loss = float(-pnl[pnl < 0].sum())
            stats.return_pct_sum = float(trades["ReturnPct"].sum()) * 100
        for key, target in [
            ("Buy & Hold Return [%]", stats.buy_hold_pct),
            ("Max. Drawdown [%]", stats.max_drawdown_pct),
            ("Exposure Time [%]", stats.exposure_pct),
        ]:
            value = result.stats.get(key)
            if value is not None and value == value:
                target.append(float(value))
        return stats

    def add(self, other: Stats) -> Stats:
        self.trades += other.trades
        self.wins += other.wins
        self.pnl += other.pnl
        self.gross_profit += other.gross_profit
        self.gross_loss += other.gross_loss
        self.return_pct_sum += other.return_pct_sum
        self.buy_hold_pct += other.buy_hold_pct
        self.max_drawdown_pct += other.max_drawdown_pct
        self.exposure_pct += other.exposure_pct
        self.errors += other.errors
        return self

    @property
    def win_rate(self) -> float | None:
        return 100 * self.wins / self.trades if self.trades else None

    @property
    def avg_trade_pct(self) -> float | None:
        return self.return_pct_sum / self.trades if self.trades else None

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss:
            return self.gross_profit / self.gross_loss
        return math.inf if self.gross_profit else None

    @property
    def expectancy(self) -> float | None:
        """P/L per trade in USD."""
        return self.pnl / self.trades if self.trades else None

    @property
    def avg_buy_hold_pct(self) -> float | None:
        return sum(self.buy_hold_pct) / len(self.buy_hold_pct) if self.buy_hold_pct else None

    def metric(self, name: str) -> float:
        value = {"profit_factor": self.profit_factor, "pnl": self.pnl, "expectancy": self.expectancy}[name]
        return -math.inf if value is None else value


@dataclass
class ConfigResult:
    index: int
    strategy: str
    params: dict[str, Any]
    label: str = ""
    rank: int | None = None
    # period name -> entry label -> stats
    results: dict[str, dict[str, Stats]] = field(default_factory=dict)

    def period_total(self, period: str) -> Stats:
        total = Stats()
        for stats in self.results.get(period, {}).values():
            total.add(stats)
        return total

    def total(self, periods: Sequence[Period]) -> Stats:
        total = Stats()
        for period in periods:
            total.add(self.period_total(period.name))
        return total

    def consistent(self, periods: Sequence[Period]) -> bool:
        """Made money in every given period that had trades."""
        totals = [self.period_total(p.name) for p in periods]
        traded = [t for t in totals if t.trades]
        return bool(traded) and all(t.pnl > 0 for t in traded)


@dataclass
class SweepOutcome:
    periods: list[Period]
    ranked: list[ConfigResult]  # every configuration, best first (tune periods only)
    finalists: list[ConfigResult]  # also run on the check periods
    defaults: ConfigResult
    baseline: ConfigResult

    @property
    def tune_periods(self) -> list[Period]:
        return [p for p in self.periods if p.role == TUNE]

    @property
    def check_periods(self) -> list[Period]:
        return [p for p in self.periods if p.role == CHECK]

    @property
    def reviewed(self) -> list[ConfigResult]:
        """Configurations with results in every period."""
        return [*self.finalists, self.defaults, self.baseline]


def parse_grid(strategy: str, assignments: Sequence[str] | None) -> dict[str, list[Any]]:
    """``["adx_min=0,20", "trend_filter=close,cross"]`` -> typed value lists."""
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
    periods: Sequence[Period],
    base_params: Mapping[str, Any] | None = None,
    cash: float = 10_000.0,
    top: int = 5,
    min_trades: int = 30,
    rank_by: str = "profit_factor",
    progress: Callable[[str], None] | None = None,
) -> SweepOutcome:
    if rank_by not in RANK_METRICS:
        raise ValueError(f"rank_by must be one of {', '.join(RANK_METRICS)}.")
    tune = [p for p in periods if p.role == TUNE]
    if not tune:
        raise ValueError("A sweep needs at least one tune period.")
    base = dict(base_params or {})
    candidates = valid_combinations(strategy, base, grid) or [base]

    configs = [ConfigResult(index, strategy, params) for index, params in enumerate(candidates, 1)]
    for config in configs:
        if progress:
            progress(f"tune {config.index}/{len(configs)}: {config.params}")
        _evaluate(config, targets, tune, cash)

    def key(config: ConfigResult) -> tuple:
        total = config.total(tune)
        return (total.trades >= min_trades, config.consistent(tune), total.metric(rank_by), total.pnl)

    ranked = sorted(configs, key=key, reverse=True)
    for position, config in enumerate(ranked, 1):
        config.rank = position
        config.label = f"#{position}"

    finalists = [c for c in ranked if c.total(tune).trades >= min_trades][:top]
    defaults = ConfigResult(0, strategy, dict(base), label="defaults")
    baseline = ConfigResult(-1, DEFAULT_STRATEGY, {}, label="sma_rsi baseline")
    for config in [*finalists, defaults, baseline]:
        if progress:
            progress(f"check: {config.label} {config.params}")
        _evaluate(config, targets, [p for p in periods if p.name not in config.results], cash)
    return SweepOutcome(list(periods), ranked, finalists, defaults, baseline)


def period_bars(target: SweepTarget, period: Period, min_bars: int) -> list[Bar]:
    """The period's bars plus enough earlier ones to warm up ``min_bars`` of indicators."""
    warm_up_start = period.start - parse_timeframe(target.timeframe).lookback(target.asset_class, min_bars)
    return [
        bar for bar in target.bars
        if bar.timestamp >= warm_up_start and (period.end is None or bar.timestamp < period.end)
    ]


def backtest_config(
    target: SweepTarget, strategy: str, params: Mapping[str, Any], period: Period, cash: float
) -> BacktestConfig:
    commission, spread = default_costs(target.asset_class)
    return BacktestConfig(
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
        start=period.start,
    )


def _evaluate(config: ConfigResult, targets: Sequence[SweepTarget], periods: Sequence[Period], cash: float) -> None:
    min_bars = get_strategy(config.strategy, config.params).min_bars
    for period in periods:
        by_entry = config.results.setdefault(period.name, {})
        for target in targets:
            bars = period_bars(target, period, min_bars)
            if not any(bar.timestamp >= period.start for bar in bars):
                by_entry[target.label] = Stats(errors=["no data in this period"])
                continue
            try:
                result = run_strategy_backtest(
                    bars, backtest_config(target, config.strategy, config.params, period, cash),
                    target.benchmark_bars, chart_features=False,
                )
            except ValueError as exc:
                by_entry[target.label] = Stats(errors=[str(exc)])
                continue
            by_entry[target.label] = Stats.from_result(result)


def result_rows(outcome: SweepOutcome) -> list[dict[str, Any]]:
    """Flat rows (configuration x period x entry) for storage."""
    rows = []
    roles = {period.name: period for period in outcome.periods}
    configs = {id(c): c for c in [*outcome.ranked, outcome.defaults, outcome.baseline]}
    for config in configs.values():
        for period_name, by_entry in config.results.items():
            period = roles[period_name]
            for entry, stats in by_entry.items():
                symbol, _, timeframe = entry.rpartition(" ")
                rows.append({
                    "config": config.index,
                    "rank": config.rank,
                    "label": config.label,
                    "strategy": config.strategy,
                    "params_json": _params_json(config.params),
                    "period": period.name,
                    "period_start": period.start,
                    "period_end": period.end,
                    "role": period.role,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "trades": stats.trades,
                    "wins": stats.wins,
                    "pnl": stats.pnl,
                    "gross_profit": stats.gross_profit,
                    "gross_loss": stats.gross_loss,
                    "return_pct_sum": stats.return_pct_sum,
                    "buy_hold_pct": stats.buy_hold_pct[0] if stats.buy_hold_pct else None,
                    "max_drawdown_pct": stats.max_drawdown_pct[0] if stats.max_drawdown_pct else None,
                    "exposure_pct": stats.exposure_pct[0] if stats.exposure_pct else None,
                    "error": "; ".join(stats.errors) or None,
                })
    return rows


def _params_json(params: Mapping[str, Any]) -> str:
    return json.dumps(dict(params), sort_keys=True)
