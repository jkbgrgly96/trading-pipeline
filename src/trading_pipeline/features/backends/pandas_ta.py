from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from trading_pipeline.features.definitions import IndicatorDefinition


def compute_pandas_ta_features(
    frame: pd.DataFrame,
    definitions: list[IndicatorDefinition],
) -> pd.DataFrame:
    output = frame.copy()
    for definition in definitions:
        kind = definition.kind.lower()
        if kind == "sma":
            _add_sma(output, definition)
        elif kind == "rsi":
            _add_rsi(output, definition)
        elif kind == "macd":
            _add_macd(output, definition)
        elif kind == "bbands":
            _add_bbands(output, definition)
        elif kind == "atr":
            _add_atr(output, definition)
        elif kind == "candles":
            _add_candles(output, definition)
        elif kind == "ema":
            _add_ema(output, definition)
        elif kind == "adx":
            _add_adx(output, definition)
        elif kind == "donchian":
            _add_donchian(output, definition)
        elif kind == "stoch":
            _add_stoch(output, definition)
        elif kind == "natr":
            _add_natr(output, definition)
        elif kind == "rvol":
            _add_relative_volume(output, definition)
        elif kind == "roc":
            _add_roc(output, definition)
        else:
            raise ValueError(f"Unsupported indicator kind: {definition.kind}")
    return output


def _add_sma(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    length = int(definition.params.get("length", 20))
    frame[definition.name] = ta.sma(frame["Close"], length=length)


def _add_rsi(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    length = int(definition.params.get("length", 14))
    frame[definition.name] = ta.rsi(frame["Close"], length=length)


def _add_macd(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    fast = int(definition.params.get("fast", 12))
    slow = int(definition.params.get("slow", 26))
    signal = int(definition.params.get("signal", 9))
    result = ta.macd(frame["Close"], fast=fast, slow=slow, signal=signal)
    if result is None:
        _add_empty_columns(frame, [definition.name, f"{definition.name}_hist", f"{definition.name}_signal"])
        return

    frame[definition.name] = _first_column_with_prefix(result, "MACD_")
    frame[f"{definition.name}_hist"] = _first_column_with_prefix(result, "MACDh_")
    frame[f"{definition.name}_signal"] = _first_column_with_prefix(result, "MACDs_")


def _add_bbands(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    length = int(definition.params.get("length", 20))
    std = float(definition.params.get("std", 2.0))
    result = ta.bbands(frame["Close"], length=length, std=std)
    names = [
        f"{definition.name}_lower",
        f"{definition.name}_middle",
        f"{definition.name}_upper",
        f"{definition.name}_bandwidth",
        f"{definition.name}_percent",
    ]
    if result is None:
        _add_empty_columns(frame, names)
        return

    prefixes = ["BBL_", "BBM_", "BBU_", "BBB_", "BBP_"]
    for name, prefix in zip(names, prefixes, strict=True):
        frame[name] = _first_column_with_prefix(result, prefix)


def _add_atr(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    length = int(definition.params.get("length", 14))
    frame[definition.name] = ta.atr(
        frame["High"],
        frame["Low"],
        frame["Close"],
        length=length,
    )


def _add_ema(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    length = int(definition.params.get("length", 20))
    frame[definition.name] = ta.ema(frame["Close"], length=length)


def _add_adx(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """``<name>`` = ADX (trend strength), ``<name>_dmp`` / ``<name>_dmn`` = +DI / -DI."""
    length = int(definition.params.get("length", 14))
    names = [definition.name, f"{definition.name}_dmp", f"{definition.name}_dmn"]
    result = ta.adx(frame["High"], frame["Low"], frame["Close"], length=length)
    if result is None:
        _add_empty_columns(frame, names)
        return
    for name, prefix in zip(names, ["ADX_", "DMP_", "DMN_"], strict=True):
        frame[name] = _first_column_with_prefix(result, prefix)


def _add_donchian(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """Channel of the last N bars *including* the current one; compare a close
    with the previous bar's channel for a breakout."""
    lower = int(definition.params.get("lower_length", 20))
    upper = int(definition.params.get("upper_length", 20))
    names = [f"{definition.name}_lower", f"{definition.name}_middle", f"{definition.name}_upper"]
    result = ta.donchian(frame["High"], frame["Low"], lower_length=lower, upper_length=upper)
    if result is None:
        _add_empty_columns(frame, names)
        return
    for name, prefix in zip(names, ["DCL_", "DCM_", "DCU_"], strict=True):
        frame[name] = _first_column_with_prefix(result, prefix)


def _add_stoch(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """``<name>_k`` / ``<name>_d``: slow stochastic %K and %D (0..100)."""
    k = int(definition.params.get("k", 14))
    d = int(definition.params.get("d", 3))
    smooth_k = int(definition.params.get("smooth_k", 3))
    names = [f"{definition.name}_k", f"{definition.name}_d"]
    result = ta.stoch(frame["High"], frame["Low"], frame["Close"], k=k, d=d, smooth_k=smooth_k)
    if result is None:
        _add_empty_columns(frame, names)
        return
    for name, prefix in zip(names, ["STOCHk_", "STOCHd_"], strict=True):
        frame[name] = _first_column_with_prefix(result, prefix)


def _add_natr(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """ATR as a percentage of the close."""
    length = int(definition.params.get("length", 14))
    frame[definition.name] = ta.natr(frame["High"], frame["Low"], frame["Close"], length=length)


def _add_relative_volume(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """Volume divided by its simple moving average (1.0 = an average bar)."""
    length = int(definition.params.get("length", 20))
    average = ta.sma(frame["Volume"], length=length)
    if average is None:
        _add_empty_columns(frame, [definition.name])
        return
    frame[definition.name] = frame["Volume"] / average.where(average > 0)


def _add_roc(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    """Rate of change of the close over ``length`` bars, in percent."""
    length = int(definition.params.get("length", 10))
    frame[definition.name] = ta.roc(frame["Close"], length=length)


def _add_candles(frame: pd.DataFrame, definition: IndicatorDefinition) -> None:
    if len(frame) < 10:
        return

    patterns = definition.params.get("patterns", "all")
    result = ta.cdl_pattern(
        frame["Open"],
        frame["High"],
        frame["Low"],
        frame["Close"],
        name=patterns,
        fillna=0,
    )
    if result is None:
        return

    for column in result.columns:
        frame[f"pattern__{_normalize_pattern_column(column)}"] = result[column]


def _first_column_with_prefix(frame: pd.DataFrame, prefix: str) -> pd.Series:
    for column in frame.columns:
        if column.startswith(prefix):
            return frame[column]
    raise KeyError(f"No pandas-ta output column starts with {prefix!r}.")


def _add_empty_columns(frame: pd.DataFrame, names: list[str]) -> None:
    for name in names:
        frame[name] = pd.NA


def _normalize_pattern_column(column: str) -> str:
    normalized = column.lower()
    if normalized.startswith("cdl_"):
        normalized = normalized[4:]
    return normalized
