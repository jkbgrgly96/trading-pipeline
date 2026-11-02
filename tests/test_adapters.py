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
