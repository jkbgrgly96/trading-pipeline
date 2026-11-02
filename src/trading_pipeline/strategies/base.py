"""The interface every strategy implements.

A strategy turns the feature history of one symbol into a ``Signal`` (BUY /
SELL / HOLD) and says where the stop and target of a new long position go.
It never sees positions or orders: ``decisions.decide`` combines the signal
with the current position, identically in the live pipeline and the backtest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar, overload

from trading_pipeline.domain import AssetClass, Signal, SignalAction
from trading_pipeline.features import (
    DEFAULT_FEATURES,
    FeatureSnapshot,
    IndicatorDefinition,
    merge_definitions,
)


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


class Strategy(ABC):
    """Signal rules plus stop/target placement for one trading idea.

    Implementations should be stateless (e.g. frozen dataclasses whose fields
    are the parameters): the same instance evaluates every bar of every
    symbol it is assigned to, live and in backtests.
    """

    #: Registry key, used in watchlist.toml, ``--strategy`` and the database.
    name: ClassVar[str]

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
