from __future__ import annotations

import json
from urllib import request

from trading_pipeline.domain import Signal
from trading_pipeline.notifications.base import Notifier
from trading_pipeline.notifications.console import format_signal


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    def send(self, signal: Signal) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": format_signal(signal),
            "disable_web_page_preview": True,
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as response:
            if response.status >= 400:
                raise RuntimeError(f"Telegram API returned HTTP {response.status}")
