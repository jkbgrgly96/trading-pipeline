import pandas as pd

from trading_pipeline.dashboard.formatting import (
    compact_dict,
    format_number,
    format_percent,
    parse_json,
)


def test_format_number_handles_missing_and_numeric_values():
    assert format_number(None) == "-"
    assert format_number(pd.NA) == "-"
    assert format_number(12.3456) == "12.35"
    assert format_number("abc") == "abc"


def test_format_percent_handles_numeric_values():
    assert format_percent(1.2345) == "1.23%"
    assert format_percent(None) == "-"


def test_parse_json_and_compact_dict():
    assert parse_json('{"rsi_14": 55.1}') == {"rsi_14": 55.1}
    assert parse_json("not json") is None
    assert compact_dict({"a": 1, "b": 2}, ["b", "c"]) == {"b": 2}
