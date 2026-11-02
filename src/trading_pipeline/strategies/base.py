"""The interface every strategy implements.

A strategy turns the feature history of one symbol into a ``Signal`` (BUY /
SELL / HOLD), says where the stop and target of a new long position go and,
optionally, how an open position is managed (trailing stop, time exit). It
never places orders or decides on its own: ``decisions.evaluate`` combines its
answers with the current position, identically live and in the backtest.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, overload

import pandas as pd

from trading_pipeline.domain import (
    AssetClass,
    Bar,
    OpenPosition,
    PositionPlan,
    Signal,
    SignalAction,
)
from trading_pipeline.features import (
    DEFAULT_FEATURES,
    FeatureSnapshot,
    IndicatorDefinition,
    compute_feature_frame,
    merge_definitions,
)
from trading_pipeline.features.frame import bars_to_ohlcv_frame


class History(Sequence[FeatureSnapshot]):
    """Feature snapshots up to and including the bar being evaluated.

    A read-only window over a precomputed list: later bars are out of reach,
    so a strategy cannot look ahead by accident. ``history[-1]`` is the bar
    being evaluated, ``history[-2]`` the one before it.
    """

    __slots__ = ("_end", "_snapshots")

    def __init__(self, snapshots: Sequence[FeatureSnapshot], end: int | None = None) -> None:
        end = len(snapshots) if end is None else end
        if not 1 <= end <= len(snapshots):
            raise ValueError(f"History end {end} is outside 1..{len(snapshots)}.")
        self._snapshots = snapshots
        self._end = end

    def __len__(self) -> int:
        return self._end

    @overload
    def __getitem__(self, index: int) -> FeatureSnapshot: ...

    @overload
    def __getitem__(self, index: slice) -> list[FeatureSnapshot]: ...

    def __getitem__(self, index: int | slice) -> FeatureSnapshot | list[FeatureSnapshot]:
        if isinstance(index, slice):
            return [self._snapshots[i] for i in range(*index.indices(self._end))]
        position = index + self._end if index < 0 else index
        if not 0 <= position < self._end:
            raise IndexError(f"History index {index} out of range (length {self._end}).")
        return self._snapshots[position]

    @property
    def latest(self) -> FeatureSnapshot:
        return self[-1]

    @property
    def previous(self) -> FeatureSnapshot:
        return self[-2]

    def since(self, timestamp: datetime, limit: int | None = None) -> list[FeatureSnapshot]:
        """Bars strictly after ``timestamp`` (e.g. a position's entry bar), oldest
        first; with ``limit``, only the most recent ``limit`` of them."""
        cutoff = _as_utc(timestamp)
        picked: list[FeatureSnapshot] = []
        for position in range(self._end - 1, -1, -1):
            snapshot = self._snapshots[position]
            if _as_utc(snapshot.timestamp) <= cutoff or (limit is not None and len(picked) >= limit):
                break
            picked.append(snapshot)
        return picked[::-1]


@dataclass(frozen=True)
class MarketContext:
    """Series from other symbols, aligned to (and forward-filled on) the bars."""

    benchmark_close: pd.Series | None = None


class Strategy(ABC):
    """Signal rules plus stop/target placement for one trading idea.

    Implementations should be stateless (e.g. frozen dataclasses whose fields
    are the parameters): the same instance evaluates every bar of every
    symbol it is assigned to, live and in backtests.
    """

    #: Registry key, used in watchlist.toml, ``--strategy`` and the database.
    name: ClassVar[str]

    #: ``low``: a stop is hit when the bar's low touches it (target: high).
    #: ``close``: only a close beyond the level counts. Strategies may make it a field.
    stop_trigger = "low"

    @property
    @abstractmethod
    def features(self) -> Sequence[IndicatorDefinition]:
        """Indicators the rules read, on top of ``DEFAULT_FEATURES``."""

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Closed bars needed before the first signal is meaningful."""

    @abstractmethod
    def signal(self, symbol: str, asset_class: AssetClass, history: History) -> Signal:
        """Evaluate ``history.latest``. A BUY should carry its stop and target."""

    @abstractmethod
    def risk_levels(
        self, entry_price: float, latest: FeatureSnapshot
    ) -> tuple[float | None, float | None]:
        """(stop_loss, take_profit) for a long entered at ``entry_price``.

        Also used for positions found in the paper account that the pipeline
        did not open itself.
        """

    def feature_definitions(self) -> list[IndicatorDefinition]:
        """Everything to compute: the dashboard's defaults plus ``features``."""
        return merge_definitions(DEFAULT_FEATURES, self.features)

    def benchmark_for(self, symbol: str, asset_class: AssetClass) -> str | None:
        """Symbol (same asset class) whose closes ``derive`` needs, e.g. SPY."""
        return None

    def derive(self, frame: pd.DataFrame, context: MarketContext) -> Mapping[str, pd.Series]:
        """Extra columns computed once over the whole frame (e.g. a score).

        They must be causal: a value at bar i may only use bars <= i, so a
        live run and a backtest see the same numbers. Read the indicator
        columns from ``features`` here; ``signal`` then reads the results
        through ``History`` like any other feature.
        """
        return {}

    def manage(self, position: OpenPosition, history: History) -> PositionPlan | None:
        """Rules for an open position at this bar's close (trailing stop, time exit)."""
        return None

    def params(self) -> dict[str, Any]:
        """Current parameter values, stored with every signal, position and backtest."""
        return dataclasses.asdict(self) if dataclasses.is_dataclass(self) else {}


def strategy_frame(
    strategy: Strategy,
    bars: list[Bar],
    benchmark_bars: list[Bar] | None = None,
    chart_features: bool = True,
) -> pd.DataFrame:
    """Indicators plus the strategy's derived columns, shared by live runs and backtests.

    ``chart_features=False`` skips ``DEFAULT_FEATURES`` (only the dashboard
    needs them), which makes parameter sweeps much faster.
    """
    definitions = strategy.feature_definitions() if chart_features else list(strategy.features)
    frame = compute_feature_frame(bars, definitions)
    benchmark = None
    if benchmark_bars:
        closes = bars_to_ohlcv_frame(benchmark_bars)["Close"]
        benchmark = closes[~closes.index.duplicated(keep="last")].reindex(frame.index, method="ffill")
    for name, series in strategy.derive(frame, MarketContext(benchmark_close=benchmark)).items():
        if name in frame.columns:
            raise ValueError(f"{strategy.name}: derived column {name!r} clashes with a feature.")
        frame[name] = series
    return frame


def _as_utc(value: datetime) -> datetime:
    value = pd.Timestamp(value).to_pydatetime()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def hold_signal(
    symbol: str,
    asset_class: AssetClass,
    latest: FeatureSnapshot,
    reason: str,
) -> Signal:
    return Signal(
        symbol=symbol,
        asset_class=asset_class,
        action=SignalAction.HOLD,
        confidence=0.0,
        reason=reason,
        latest_close=latest.close,
        timestamp=latest.timestamp,
    )
