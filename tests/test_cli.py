import pytest

from fakes import FakeBroker, after_close, crossover_bars
from trading_pipeline import cli
from trading_pipeline.brokers import alpaca


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env / watchlist.toml
    for key, value in {
        "ALPACA_API_KEY": "k",
        "ALPACA_SECRET_KEY": "s",
        "DATABASE_PATH": str(tmp_path / "db.sqlite3"),
        "TRADING_MODE": "alerts_only",
        "PLACE_ORDERS": "false",
    }.items():
        monkeypatch.setenv(key, value)
    bars = crossover_bars()
    broker = FakeBroker(bars)
    monkeypatch.setattr(alpaca, "AlpacaBroker", lambda **kwargs: broker)
    monkeypatch.setattr("trading_pipeline.watch.datetime", _FrozenDatetime(after_close(bars)))
    return broker


class _FrozenDatetime:
    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now

    def __call__(self, *args, **kwargs):
        from datetime import datetime

        return datetime(*args, **kwargs)


def test_once_without_watchlist_uses_cli_symbol(env, capsys):
    code = cli.main(["--once", "--symbol", "TEST", "--timeframe", "1Day"])
    output = capsys.readouterr().out
    assert code == 0
    assert "BUY TEST" in output
    assert "TEST 1Day (stock): enter_long" in output
    assert env.buys == []  # alerts only


def test_invalid_timeframe_is_rejected(env):
    with pytest.raises(SystemExit):
        cli.main(["--timeframe", "2Day"])


def test_telegram_without_credentials_exits(env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--once", "--telegram"])
