from __future__ import annotations

import json
from typing import Any

import pandas as pd


def format_number(value: Any, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value: Any, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    try:
        return f"{float(value):.{decimals}f}%"
    except (TypeError, ValueError):
        return str(value)


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def compact_dict(value: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    if not value:
        return {}
    return {key: value.get(key) for key in keys if key in value}
