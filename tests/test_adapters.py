import io
import json
from urllib import error

import pytest

from fakes import RecordingNotifier
from trading_pipeline.brokers.alpaca import AlpacaBroker
from trading_pipeline.brokers.base import LiveTradingRefusedError
from trading_pipeline.domain import AssetClass
from trading_pipeline.notifications import telegram
from trading_pipeline.notifications.base import MultiNotifier, NotificationError
from trading_pipeline.notifications.telegram import TelegramNotifier


def test_alpaca_live_keys_never_get_a_trading_client():
    broker = AlpacaBroker("key", "secret", paper=False)
    assert broker.is_paper is False
    with pytest.raises(LiveTradingRefusedError):
        broker.submit_market_buy("AAPL", AssetClass.STOCK, 10)
    with pytest.raises(LiveTradingRefusedError):
        broker.close_position("AAPL", AssetClass.STOCK)
    with pytest.raises(LiveTradingRefusedError):
        broker.get_position("AAPL", AssetClass.STOCK)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_telegram_success(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(telegram.request, "urlopen", fake_urlopen)
    TelegramNotifier("TOKEN123", "42").send_message("hello")
    assert sent["body"]["chat_id"] == "42" and sent["body"]["text"] == "hello"


def test_telegram_http_error_is_reported_without_token(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"description": "chat not found"}')
        )

    monkeypatch.setattr(telegram.request, "urlopen", fake_urlopen)
    with pytest.raises(NotificationError) as excinfo:
        TelegramNotifier("SECRETTOKEN", "42").send_message("hello")
    assert "chat not found" in str(excinfo.value)
    assert "SECRETTOKEN" not in str(excinfo.value)


def test_multi_notifier_optional_failure_does_not_block():
    required, optional = RecordingNotifier(), RecordingNotifier(failures=1)
    MultiNotifier([required], optional=[optional]).send_message("x")
    assert required.messages == ["x"]


def test_multi_notifier_required_failure_raises():
    with pytest.raises(NotificationError):
        MultiNotifier([RecordingNotifier(failures=1)]).send_message("x")


def _capture_bar_requests(broker, monkeypatch):
    from types import SimpleNamespace

    requests = []

    def fake(request):
        requests.append(request)
        return SimpleNamespace(data={})

    monkeypatch.setattr(broker._stock_data_client, "get_stock_bars", fake)
    monkeypatch.setattr(broker._crypto_data_client, "get_crypto_bars", fake)
    return requests


def test_alpaca_stock_bars_are_split_adjusted(monkeypatch):
    from datetime import UTC, datetime

    from alpaca.common.enums import Sort
    from alpaca.data.enums import Adjustment

    broker = AlpacaBroker("key", "secret", paper=False)
    requests = _capture_bar_requests(broker, monkeypatch)
    start, end = datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)

    broker.get_bars("AAPL", AssetClass.STOCK, "1Day", 10)
    broker.get_bars_between("AAPL", AssetClass.STOCK, "1Day", start, end)

    latest, ranged = requests
    assert latest.adjustment == ranged.adjustment == Adjustment.SPLIT
    assert (latest.limit, latest.sort) == (10, Sort.DESC)
    assert (ranged.limit, ranged.sort) == (None, Sort.ASC)
    # The SDK keeps request times as naive UTC.
    assert (ranged.start, ranged.end) == (start.replace(tzinfo=None), end.replace(tzinfo=None))


def test_alpaca_rejects_unknown_adjustment():
    with pytest.raises(ValueError, match="adjustment"):
        AlpacaBroker("key", "secret", paper=False, adjustment="bogus")


@pytest.mark.parametrize(
    ("filled_qty", "filled_avg_price", "expected_qty", "expected_price"),
    [("0.5", "101.25", 0.5, 101.25), ("0", "0", 0.0, None), (None, None, 0.0, None)],
)
def test_alpaca_order_fill_details(monkeypatch, filled_qty, filled_avg_price, expected_qty, expected_price):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from alpaca.trading.enums import OrderStatus

    filled_at = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    broker = AlpacaBroker("key", "secret", paper=True)
    order = SimpleNamespace(
        id="abc", status=OrderStatus.PARTIALLY_FILLED, filled_qty=filled_qty,
        filled_avg_price=filled_avg_price, filled_at=filled_at,
    )
    monkeypatch.setattr(broker._trading_client, "get_order_by_id", lambda order_id: order)

    result = broker.get_order("abc")

    assert (result.status, result.filled_qty, result.filled_avg_price) == (
        "partially_filled", expected_qty, expected_price,
    )
    assert broker.get_order_status("abc") == "partially_filled"


def test_sip_requests_end_before_the_free_plan_delay(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from trading_pipeline.brokers.alpaca import SIP_DELAY

    broker = AlpacaBroker("key", "secret", paper=False, stock_feed="sip")
    requests = _capture_bar_requests(broker, monkeypatch)

    broker.get_bars("AAPL", AssetClass.STOCK, "1Day", 10)
    broker.get_bars("BTC/USD", AssetClass.CRYPTO, "1Hour", 10)

    latest_allowed = (datetime.now(UTC) - SIP_DELAY).replace(tzinfo=None)
    stock, crypto = requests
    assert stock.end <= latest_allowed
    assert crypto.end > latest_allowed  # crypto data is not delayed
    assert broker.data_delay(AssetClass.STOCK) == SIP_DELAY
    assert broker.data_delay(AssetClass.CRYPTO) == timedelta(0)


def test_iex_requests_are_not_delayed():
    from datetime import timedelta

    broker = AlpacaBroker("key", "secret", paper=False, stock_feed="iex")
    assert broker.data_delay(AssetClass.STOCK) == timedelta(0)
