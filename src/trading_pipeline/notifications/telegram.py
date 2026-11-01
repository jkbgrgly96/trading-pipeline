from __future__ import annotations

import json
from urllib import error, request

from trading_pipeline.notifications.base import NotificationError, Notifier

TELEGRAM_MAX_LENGTH = 4096


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout

    def send_message(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text[:TELEGRAM_MAX_LENGTH],
            "disable_web_page_preview": True,
        }
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except error.HTTPError as exc:
            # urlopen raises for 4xx/5xx; surface Telegram's description but
            # never the URL, which contains the bot token.
            description = _description(exc)
            raise NotificationError(f"Telegram HTTP {exc.code}: {description}") from None
        except (error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NotificationError(f"Telegram unreachable: {reason}") from None
        if not body.get("ok", False):
            raise NotificationError(f"Telegram rejected message: {body.get('description')}")


def _description(exc: error.HTTPError) -> str:
    try:
        return json.loads(exc.read().decode("utf-8")).get("description", exc.reason)
    except Exception:  # noqa: BLE001
        return str(exc.reason)
