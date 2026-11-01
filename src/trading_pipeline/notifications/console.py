from __future__ import annotations

import sys

from trading_pipeline.notifications.base import Notifier
from trading_pipeline.notifications.formatting import format_signal

__all__ = ["ConsoleNotifier", "format_signal"]


class ConsoleNotifier(Notifier):
    def send_message(self, text: str) -> None:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            # Windows consoles/redirects may not be UTF-8; drop what can't print.
            encoding = sys.stdout.encoding or "ascii"
            print(text.encode(encoding, errors="replace").decode(encoding), flush=True)
        print("-" * 40, flush=True)
