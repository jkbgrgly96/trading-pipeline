from __future__ import annotations

import pandas as pd

from trading_pipeline.domain import Bar


def bars_to_ohlcv_frame(bars: list[Bar]) -> pd.DataFrame:
    if not bars:
        raise ValueError("Cannot build an OHLCV frame from an empty bar list.")

    frame = pd.DataFrame(
        {
            "Open": [bar.open for bar in bars],
            "High": [bar.high for bar in bars],
            "Low": [bar.low for bar in bars],
            "Close": [bar.close for bar in bars],
            "Volume": [bar.volume for bar in bars],
        },
        index=pd.DatetimeIndex([bar.timestamp for bar in bars], name="Timestamp"),
    )
    return frame.sort_index()
