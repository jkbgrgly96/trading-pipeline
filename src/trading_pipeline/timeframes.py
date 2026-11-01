from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from trading_pipeline.domain import AssetClass, Bar


class TimeframeUnit(str, Enum):
    MINUTE = "Min"
    HOUR = "Hour"
    DAY = "Day"


_UNIT_ALIASES = {
    "m": TimeframeUnit.MINUTE,
    "min": TimeframeUnit.MINUTE,
    "minute": TimeframeUnit.MINUTE,
    "t": TimeframeUnit.MINUTE,
    "h": TimeframeUnit.HOUR,
    "hour": TimeframeUnit.HOUR,
    "d": TimeframeUnit.DAY,
    "day": TimeframeUnit.DAY,
}

# Alpaca limits: 1-59 minutes, 1-23 hours, 1 day.
_MAX_AMOUNT = {
    TimeframeUnit.MINUTE: 59,
    TimeframeUnit.HOUR: 23,
    TimeframeUnit.DAY: 1,
}

_PATTERN = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Timeframe:
    amount: int
    unit: TimeframeUnit

    @property
    def duration(self) -> timedelta:
        if self.unit == TimeframeUnit.MINUTE:
            return timedelta(minutes=self.amount)
        if self.unit == TimeframeUnit.HOUR:
            return timedelta(hours=self.amount)
        return timedelta(days=self.amount)

    @property
    def label(self) -> str:
        return f"{self.amount}{self.unit.value}"

    def to_alpaca(self) -> Any:
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        unit = {
            TimeframeUnit.MINUTE: TimeFrameUnit.Minute,
            TimeframeUnit.HOUR: TimeFrameUnit.Hour,
            TimeframeUnit.DAY: TimeFrameUnit.Day,
        }[self.unit]
        return TimeFrame(self.amount, unit)

    def lookback(self, asset_class: AssetClass, limit: int) -> timedelta:
        """Calendar window wide enough to contain ``limit`` bars.

        Requests are sorted newest-first and capped by ``limit``, so a generous
        window costs nothing; a short one silently returns too few bars
        (weekends, holidays, and stock sessions that cover only part of a day).
        """
        if asset_class == AssetClass.CRYPTO:
            return self.duration * limit * 1.5 + timedelta(days=2)
        if self.unit == TimeframeUnit.DAY:
            # ~252 trading days per 365 calendar days, plus holiday slack.
            return self.duration * limit * 1.6 + timedelta(days=10)
        # Intraday stock bars: a session covers well under half a day and
        # only five days a week; 6x plus a long-weekend buffer is safe.
        return self.duration * limit * 6 + timedelta(days=5)


def parse_timeframe(value: str) -> Timeframe:
    match = _PATTERN.match(value or "")
    if not match:
        raise ValueError(f"Unsupported timeframe: {value!r}. Use e.g. 5Min, 1Hour, 1Day.")
    amount = int(match.group(1))
    unit = _UNIT_ALIASES.get(match.group(2).lower())
    if unit is None:
        raise ValueError(f"Unsupported timeframe unit in {value!r}. Use Min, Hour, or Day.")
    if not 1 <= amount <= _MAX_AMOUNT[unit]:
        raise ValueError(
            f"Unsupported timeframe {value!r}: {unit.value} amount must be "
            f"between 1 and {_MAX_AMOUNT[unit]}."
        )
    return Timeframe(amount, unit)


def closed_bars(bars: list[Bar], timeframe: Timeframe, now: datetime) -> list[Bar]:
    """Drop bars whose period has not finished yet.

    Alpaca bar timestamps mark the *start* of the bar, and the newest bar
    returned during a session is still forming. Acting on it would make
    signals appear and disappear within the bar and would not match the
    backtest, which only ever sees completed bars.
    """
    duration = timeframe.duration
    return [bar for bar in bars if bar.timestamp + duration <= now]
