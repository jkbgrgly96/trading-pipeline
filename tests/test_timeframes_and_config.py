from datetime import UTC, datetime, timedelta

import pytest

from trading_pipeline.config import Settings, parse_env_line
from trading_pipeline.domain import AssetClass, Bar
from trading_pipeline.timeframes import TimeframeUnit, closed_bars, parse_timeframe


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("1Min", 1, TimeframeUnit.MINUTE),
        ("15min", 15, TimeframeUnit.MINUTE),
        ("1Hour", 1, TimeframeUnit.HOUR),
        ("4h", 4, TimeframeUnit.HOUR),
        ("1Day", 1, TimeframeUnit.DAY),
        ("1d", 1, TimeframeUnit.DAY),
    ],
)
def test_parse_timeframe(text, amount, unit):
    parsed = parse_timeframe(text)
    assert (parsed.amount, parsed.unit) == (amount, unit)


@pytest.mark.parametrize("text", ["", "Day", "2Day", "60Min", "1Week", "abc"])
def test_parse_timeframe_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_timeframe(text)


def test_closed_bars_drops_the_forming_bar():
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars = [Bar(start + timedelta(minutes=i), 1, 1, 1, 1, 1) for i in range(3)]
    now = start + timedelta(minutes=2, seconds=30)  # third bar still open
    assert closed_bars(bars, parse_timeframe("1Min"), now) == bars[:2]


def test_stock_minute_lookback_covers_a_weekend():
    # 250 one-minute bars on a Monday morning need data from Friday.
    lookback = parse_timeframe("1Min").lookback(AssetClass.STOCK, 250)
    assert lookback >= timedelta(days=4)


def test_daily_lookback_covers_trading_calendar():
    lookback = parse_timeframe("1Day").lookback(AssetClass.STOCK, 250)
    assert lookback >= timedelta(days=365)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("KEY=value", ("KEY", "value")),
        ("KEY=value # comment", ("KEY", "value")),
        ('KEY="quoted # not comment" # c', ("KEY", "quoted # not comment")),
        ("KEY='single'", ("KEY", "single")),
        ("export KEY=1", ("KEY", "1")),
        ("URL=http://x/#frag", ("URL", "http://x/#frag")),
        ("# comment", None),
        ("", None),
        ("NOEQUALS", None),
    ],
)
def test_parse_env_line(line, expected):
    assert parse_env_line(line) == expected


@pytest.mark.parametrize(
    ("paper", "mode", "place", "enabled"),
    [
        (True, "paper", True, True),
        (False, "paper", True, False),
        (True, "alerts_only", True, False),
        (True, "paper", False, False),
        (True, "live", True, False),
    ],
)
def test_orders_require_all_three_paper_switches(paper, mode, place, enabled):
    settings = Settings("k", "s", alpaca_paper=paper, trading_mode=mode, place_orders=place)
    assert settings.orders_enabled is enabled
