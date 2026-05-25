import pandas as pd

from trading_pipeline.dashboard.app import _date_text, _jsonable_row, _select_columns


def test_select_columns_ignores_missing_columns():
    frame = pd.DataFrame({"a": [1], "b": [2]})

    selected = _select_columns(frame, ["b", "c"])

    assert list(selected.columns) == ["b"]


def test_jsonable_row_converts_datetimes_and_missing_values():
    row = pd.Series(
        {
            "timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
            "value": pd.NA,
            "features": {"rsi_14": 50},
        }
    )

    output = _jsonable_row(row)

    assert output["timestamp"].startswith("2026-01-01")
    assert output["value"] is None
    assert output["features"] == {"rsi_14": 50}


def test_date_text_handles_missing_and_timestamp_values():
    assert _date_text(pd.NA) == "-"
    assert _date_text(pd.Timestamp("2026-01-01")) == "2026-01-01"
