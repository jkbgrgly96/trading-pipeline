from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_env_line(raw_line: str) -> tuple[str, str] | None:
    """Parse one ``KEY=value`` line; supports quotes and trailing ``# comments``."""
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if value[:1] in {'"', "'"}:
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end != -1 else value[1:]
    else:
        # Unquoted: an inline comment starts at " #" (a bare "#" may be part
        # of a value, e.g. a URL fragment).
        comment = value.find(" #")
        if comment != -1:
            value = value[:comment]
        value = value.strip()
    return key, value


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(raw_line)
        if parsed:
            key, value = parsed
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool = True
    alpaca_stock_feed: str = "iex"
    alpaca_adjustment: str = "split"
    trading_mode: str = "alerts_only"
    place_orders: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    default_symbol: str = "AAPL"
    default_asset_class: str = "stock"
    default_timeframe: str = "1Day"
    default_strategy: str = "sma_rsi"
    default_bar_limit: int = 250
    default_notional: float = 100.0
    database_path: str = "data/trading_pipeline.sqlite3"
    watchlist_path: str = "watchlist.toml"
    notify_hold: bool = False
    # Skip BUY signals while this many positions are open (None: no limit).
    max_open_positions: int | None = None

    @property
    def orders_enabled(self) -> bool:
        """Paper orders only. All three switches must be set explicitly."""
        return self.alpaca_paper and self.trading_mode == "paper" and self.place_orders


def load_settings() -> Settings:
    load_env_file()
    return Settings(
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_paper=_as_bool(os.getenv("ALPACA_PAPER"), True),
        alpaca_stock_feed=os.getenv("ALPACA_STOCK_FEED", "iex").strip().lower(),
        alpaca_adjustment=os.getenv("ALPACA_ADJUSTMENT", "split").strip().lower(),
        trading_mode=os.getenv("TRADING_MODE", "alerts_only").strip().lower(),
        place_orders=_as_bool(os.getenv("PLACE_ORDERS"), False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        default_symbol=os.getenv("DEFAULT_SYMBOL", "AAPL"),
        default_asset_class=os.getenv("DEFAULT_ASSET_CLASS", "stock"),
        default_timeframe=os.getenv("DEFAULT_TIMEFRAME", "1Day"),
        default_strategy=os.getenv("DEFAULT_STRATEGY", "sma_rsi").strip(),
        default_bar_limit=int(os.getenv("DEFAULT_BAR_LIMIT", "250")),
        default_notional=float(os.getenv("DEFAULT_NOTIONAL", "100")),
        database_path=os.getenv("DATABASE_PATH", "data/trading_pipeline.sqlite3"),
        watchlist_path=os.getenv("WATCHLIST_PATH", "watchlist.toml"),
        notify_hold=_as_bool(os.getenv("NOTIFY_HOLD"), False),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS") or 0) or None,
    )
