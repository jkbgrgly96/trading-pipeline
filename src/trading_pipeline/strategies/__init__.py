"""Strategy registry: names used in watchlist.toml, ``--strategy`` and the database."""

from __future__ import annotations

from trading_pipeline.strategies.base import History, Strategy, hold_signal
from trading_pipeline.strategies.sma_rsi import SmaRsiStrategy

DEFAULT_STRATEGY = SmaRsiStrategy.name

# Register new strategies here.
STRATEGIES: dict[str, type[Strategy]] = {
    SmaRsiStrategy.name: SmaRsiStrategy,
}


def available_strategies() -> list[str]:
    return sorted(STRATEGIES)


def get_strategy(name: str) -> Strategy:
    """A strategy instance with its default parameters."""
    try:
        strategy_class = STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy {name!r}. Available: {', '.join(available_strategies())}."
        ) from None
    return strategy_class()


__all__ = [
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "History",
    "SmaRsiStrategy",
    "Strategy",
    "available_strategies",
    "get_strategy",
    "hold_signal",
]
