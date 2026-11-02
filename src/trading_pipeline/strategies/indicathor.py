"""IndicaThor: the user's composite momentum score, rebuilt for this pipeline.

Two flags, each -2..+2:

* RSI flag (level): <= 30 -> -2, < 50 -> -1, > 50 -> +1, >= 70 -> +2.
* MACD flag. ``cross`` (default): the original event flag (+1 after a bullish
  cross below zero, -1 after a bearish cross above zero, 0 after a cross on the
  "wrong" side of zero, otherwise unchanged). ``state``: above its signal line
  and above zero -> +2, above the signal only -> +1, below the signal -> -1,
  below and under zero -> -2. In the 2021-2023 / 2024-2026 sweep, ``cross``
  traded far less but had the better and steadier profit per trade.

score = w_macd * MACD flag + w_rsi * RSI flag, smoothed with an EMA.

Long entry: smoothed score >= ``entry_score``, both flags positive, plus the
trend filter (fast EMA above slow EMA by default, or close above the slow EMA), ADX
trend strength and, optionally, relative volume and relative strength against
a benchmark (stocks). Exit: smoothed score < ``exit_score`` or both flags <= 0,
or the stop. The stop starts at entry - ``stop_atr_mult`` x ATR and then trails
up to the highest (high - ``trail_atr_mult`` x ATR) of up to ``trail_lookback``
bars since entry; it never moves down.

All indicators come from pandas-ta-classic; only the flag rules are local.

Defaults come from the 2026-11 sweep (tuned on 2016-2023, checked on 2024-2026):
the EMA 50 > EMA 200 trend filter and a 10-bar score EMA were shared by every
top configuration and lifted the 2024-2026 profit factor from 1.31 to 1.81.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from trading_pipeline.domain import AssetClass, OpenPosition, PositionPlan, Signal, SignalAction
from trading_pipeline.features import FeatureSnapshot, IndicatorDefinition
from trading_pipeline.strategies.base import History, MarketContext, Strategy, hold_signal

RSI_FLAG = "ithor_rsi_flag"
MACD_FLAG = "ithor_macd_flag"
SCORE = "ithor_score"
SCORE_EMA = "ithor_score_ema"
RELATIVE_STRENGTH = "ithor_rs"


@dataclass(frozen=True)
class IndicathorStrategy(Strategy):
    name: ClassVar[str] = "indicathor"

    rsi_length: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_flag_mode: str = "cross"  # "cross" | "state"
    w_rsi: float = 1.0
    w_macd: float = 1.0
    score_ema: int = 10
    entry_score: float = 1.0
    exit_score: float = 0.0
    trend_filter: str = "cross"  # "cross": EMA fast > EMA slow | "close": close > EMA slow | "none"
    ema_fast: int = 50
    ema_slow: int = 200
    adx_length: int = 14
    adx_min: float = 20.0  # 0 disables the ADX filter
    rvol_length: int = 20
    rvol_min: float = 0.0  # 0 disables the relative-volume filter
    use_rs: bool = False  # relative strength against rs_benchmark (stocks only)
    rs_benchmark: str = "SPY"
    rs_length: int = 126
    stop_atr_length: int = 14
    stop_atr_mult: float = 2.0
    trail_atr_length: int = 10
    trail_atr_mult: float = 3.0
    trail_lookback: int = 20
    stop_trigger: str = "low"  # "low" | "close"

    def __post_init__(self) -> None:
        if self.macd_flag_mode not in {"state", "cross"}:
            raise ValueError("indicathor: macd_flag_mode must be 'state' or 'cross'.")
        if self.trend_filter not in {"close", "cross", "none"}:
            raise ValueError("indicathor: trend_filter must be 'close', 'cross' or 'none'.")
        if self.stop_trigger not in {"low", "close"}:
            raise ValueError("indicathor: stop_trigger must be 'low' or 'close'.")
        if not 0 < self.macd_fast < self.macd_slow or not 0 < self.ema_fast < self.ema_slow:
            raise ValueError("indicathor: fast lengths must be below slow lengths.")
        lengths = [
            self.rsi_length, self.macd_signal, self.score_ema, self.adx_length, self.rvol_length,
            self.rs_length, self.stop_atr_length, self.trail_atr_length, self.trail_lookback,
        ]
        if min(lengths) < 1:
            raise ValueError("indicathor: lengths must be positive.")
        if self.entry_score <= self.exit_score:
            raise ValueError("indicathor: entry_score must be above exit_score.")
        if self.stop_atr_mult <= 0 or self.trail_atr_mult <= 0:
            raise ValueError("indicathor: ATR multipliers must be positive.")

    # --- feature names ---------------------------------------------------

    @property
    def _rsi(self) -> str:
        return f"rsi_{self.rsi_length}"

    @property
    def _macd(self) -> str:
        return f"macd_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}"

    @property
    def _ema_fast(self) -> str:
        return f"ema_{self.ema_fast}"

    @property
    def _ema_slow(self) -> str:
        return f"ema_{self.ema_slow}"

    @property
    def _adx(self) -> str:
        return f"adx_{self.adx_length}"

    @property
    def _rvol(self) -> str:
        return f"rvol_{self.rvol_length}"

    @property
    def _roc(self) -> str:
        return f"roc_{self.rs_length}"

    @property
    def _stop_atr(self) -> str:
        return f"atr_{self.stop_atr_length}"

    @property
    def _trail_atr(self) -> str:
        return f"atr_{self.trail_atr_length}"

    @property
    def features(self) -> list[IndicatorDefinition]:
        definitions = [
            IndicatorDefinition(self._rsi, "rsi", {"length": self.rsi_length}),
            IndicatorDefinition(
                self._macd, "macd",
                {"fast": self.macd_fast, "slow": self.macd_slow, "signal": self.macd_signal},
            ),
            IndicatorDefinition(self._ema_fast, "ema", {"length": self.ema_fast}),
            IndicatorDefinition(self._ema_slow, "ema", {"length": self.ema_slow}),
            IndicatorDefinition(self._adx, "adx", {"length": self.adx_length}),
            IndicatorDefinition(self._rvol, "rvol", {"length": self.rvol_length}),
            IndicatorDefinition(self._stop_atr, "atr", {"length": self.stop_atr_length}),
        ]
        if self.trail_atr_length != self.stop_atr_length:
            definitions.append(IndicatorDefinition(self._trail_atr, "atr", {"length": self.trail_atr_length}))
        if self.use_rs:
            definitions.append(IndicatorDefinition(self._roc, "roc", {"length": self.rs_length}))
        return definitions

    @property
    def min_bars(self) -> int:
        warm_up = [
            self.macd_slow + self.macd_signal,
            self.rsi_length + 1,
            2 * self.adx_length,
            self.rvol_length,
            self.stop_atr_length + 1,
            self.trail_atr_length + 1,
        ]
        if self.trend_filter != "none":
            warm_up.append(self.ema_slow)
        if self.use_rs:
            warm_up.append(self.rs_length + 1)
        return max(warm_up) + self.score_ema + 5

    # --- derived columns (computed once over all bars, causal) -----------------

    def benchmark_for(self, symbol: str, asset_class: AssetClass) -> str | None:
        if self.use_rs and asset_class == AssetClass.STOCK and symbol != self.rs_benchmark:
            return self.rs_benchmark
        return None

    def derive(self, frame: pd.DataFrame, context: MarketContext) -> Mapping[str, pd.Series]:
        rsi_flag = _rsi_flag(frame[self._rsi])
        macd, signal = frame[self._macd], frame[f"{self._macd}_signal"]
        macd_flag = _macd_state_flag(macd, signal) if self.macd_flag_mode == "state" else _macd_cross_flag(macd, signal)
        score = self.w_macd * macd_flag + self.w_rsi * rsi_flag
        valid = score.dropna()
        score_ema = (
            ta.ema(valid, length=self.score_ema).reindex(frame.index)
            if len(valid) >= self.score_ema
            else pd.Series(np.nan, index=frame.index)
        )
        derived = {RSI_FLAG: rsi_flag, MACD_FLAG: macd_flag, SCORE: score, SCORE_EMA: score_ema}
        if self.use_rs and context.benchmark_close is not None:
            benchmark_roc = ta.roc(context.benchmark_close, length=self.rs_length)
            derived[RELATIVE_STRENGTH] = frame[self._roc] - benchmark_roc
        return derived

    # --- rules -------------------------------------------------------------------

    def signal(self, symbol: str, asset_class: AssetClass, history: History) -> Signal:
        latest = history.latest
        score_ema, rsi_flag, macd_flag = latest.get(SCORE_EMA), latest.get(RSI_FLAG), latest.get(MACD_FLAG)
        if score_ema is None or rsi_flag is None or macd_flag is None:
            return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
        flags = f"score={score_ema:+.2f}, rsi_flag={rsi_flag:+.0f}, macd_flag={macd_flag:+.0f}"

        if score_ema < self.exit_score or (rsi_flag <= 0 and macd_flag <= 0):
            reason = "score_below_exit" if score_ema < self.exit_score else "both_flags_non_positive"
            return Signal(
                symbol, asset_class, SignalAction.SELL, 0.6, f"{reason}, {flags}",
                latest.close, latest.timestamp,
            )

        if score_ema >= self.entry_score and rsi_flag > 0 and macd_flag > 0:
            blocked = self._blocked_by_filters(latest)
            if blocked:
                return hold_signal(symbol, asset_class, latest, f"entry_filtered_{blocked}, {flags}")
            stop_loss, take_profit = self.risk_levels(latest.close, latest)
            if stop_loss is None:
                return hold_signal(symbol, asset_class, latest, "insufficient_indicator_data")
            confidence = min(0.95, 0.5 + 0.1 * score_ema)
            return Signal(
                symbol, asset_class, SignalAction.BUY, confidence, f"score_entry, {flags}",
                latest.close, latest.timestamp, stop_loss, take_profit,
            )
        return hold_signal(symbol, asset_class, latest, f"no_setup, {flags}")

    def _blocked_by_filters(self, latest: FeatureSnapshot) -> str | None:
        """Name of the first filter that blocks an entry (missing data blocks too)."""
        if self.trend_filter != "none":
            slow = latest.get(self._ema_slow)
            reference = latest.close if self.trend_filter == "close" else latest.get(self._ema_fast)
            if slow is None or reference is None or reference <= slow:
                return "trend"
        if self.adx_min > 0:
            adx = latest.get(self._adx)
            if adx is None or adx < self.adx_min:
                return "adx"
        if self.rvol_min > 0:
            rvol = latest.get(self._rvol)
            if rvol is None or rvol < self.rvol_min:
                return "volume"
        if self.use_rs:
            # No benchmark (crypto, or the benchmark itself): nothing to compare against.
            rs = latest.get(RELATIVE_STRENGTH)
            if RELATIVE_STRENGTH in latest.values and (rs is None or rs <= 0):
                return "relative_strength"
        return None

    def risk_levels(self, entry_price: float, latest: FeatureSnapshot) -> tuple[float | None, float | None]:
        atr = latest.get(self._stop_atr)
        if atr is None:
            return None, None
        return entry_price - self.stop_atr_mult * atr, None

    def manage(self, position: OpenPosition, history: History) -> PositionPlan | None:
        held = history.since(position.entry_time, limit=self.trail_lookback)
        candidates = [
            snapshot.high - self.trail_atr_mult * atr
            for snapshot in held
            if (atr := snapshot.get(self._trail_atr)) is not None
        ]
        return PositionPlan(stop_loss=max(candidates)) if candidates else None


def _rsi_flag(rsi: pd.Series) -> pd.Series:
    flag = np.select([rsi <= 30, rsi < 50, rsi >= 70, rsi > 50], [-2.0, -1.0, 2.0, 1.0], default=0.0)
    return pd.Series(flag, index=rsi.index).where(rsi.notna())


def _macd_state_flag(macd: pd.Series, signal: pd.Series) -> pd.Series:
    above = macd > signal
    flag = np.select(
        [above & (macd > 0), above, macd < 0],
        [2.0, 1.0, -2.0],
        default=-1.0,
    )
    return pd.Series(flag, index=macd.index).where(macd.notna() & signal.notna())


def _macd_cross_flag(macd: pd.Series, signal: pd.Series) -> pd.Series:
    prev_macd, prev_signal = macd.shift(), signal.shift()
    cross_up = (macd > signal) & (prev_macd <= prev_signal)
    cross_down = (macd < signal) & (prev_macd >= prev_signal)
    event = np.select(
        [
            cross_up & (prev_macd < 0) & (prev_signal < 0),
            cross_down & (prev_macd > 0) & (prev_signal > 0),
            (cross_up & (prev_macd > 0)) | (cross_down & (prev_macd < 0)),
        ],
        [1.0, -1.0, 0.0],
        default=np.nan,
    )
    flag = pd.Series(event, index=macd.index).ffill().fillna(0.0)
    return flag.where(macd.notna() & signal.notna())
