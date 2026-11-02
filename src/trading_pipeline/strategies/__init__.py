"""Strategy registry: names used in watchlist.toml, ``--strategy`` and the database."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from trading_pipeline.strategies.base import (
    History,
    MarketContext,
    Strategy,
    hold_signal,
    strategy_frame,
)
from trading_pipeline.strategies.sma_rsi import SmaRsiStrategy

DEFAULT_STRATEGY = SmaRsiStrategy.name

# Register new strategies here.
STRATEGIES: dict[str, type[Strategy]] = {
    SmaRsiStrategy.name: SmaRsiStrategy,
}


def available_strategies() -> list[str]:
    return sorted(STRATEGIES)


def get_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    """A strategy instance; ``params`` override its default parameters.

    Values may be typed (from TOML) or strings (from ``--param key=value``);
    unknown names and values of the wrong type raise ``ValueError``.
    """
    try:
        strategy_class = STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy {name!r}. Available: {', '.join(available_strategies())}."
        ) from None
    if not params:
        return strategy_class()
    return strategy_class(**coerce_params(strategy_class, name, params))


def coerce_params(strategy_class: type, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        {field.name: field for field in dataclasses.fields(strategy_class)}
        if dataclasses.is_dataclass(strategy_class)
        else {}
    )
    coerced = {}
    for key, value in params.items():
        if key not in fields:
            known = ", ".join(sorted(fields)) or "none"
            raise ValueError(f"Strategy {name!r} has no parameter {key!r}. Parameters: {known}.")
        coerced[key] = _coerce(name, key, str(fields[key].type), value)
    return coerced


def parse_param_assignments(assignments: list[str] | None) -> dict[str, str]:
    """``["a=1", "b=x"]`` from the command line -> ``{"a": "1", "b": "x"}``."""
    params = {}
    for assignment in assignments or []:
        key, sep, value = assignment.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--param must look like key=value, got {assignment!r}.")
        params[key.strip()] = value.strip()
    return params


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _coerce(name: str, key: str, type_name: str, value: Any) -> Any:
    def wrong() -> ValueError:
        return ValueError(f"Parameter {key!r} of {name!r} must be {type_name}, got {value!r}.")

    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise wrong()
    if type_name == "int":
        if isinstance(value, bool):
            raise wrong()
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                raise wrong() from None
        raise wrong()
    if type_name == "float":
        if isinstance(value, bool):
            raise wrong()
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                raise wrong() from None
        raise wrong()
    if type_name == "str":
        if isinstance(value, str):
            return value
        raise wrong()
    raise ValueError(f"Parameter {key!r} of {name!r} has unsupported type {type_name}.")


__all__ = [
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "History",
    "MarketContext",
    "SmaRsiStrategy",
    "Strategy",
    "available_strategies",
    "coerce_params",
    "get_strategy",
    "hold_signal",
    "parse_param_assignments",
    "strategy_frame",
]
