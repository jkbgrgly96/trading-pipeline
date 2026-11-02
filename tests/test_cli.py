from datetime import UTC, datetime

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


def test_unknown_strategy_is_rejected(env):
    with pytest.raises(SystemExit):
        cli.main(["--once", "--strategy", "nope"])


def test_once_reports_the_strategy_in_the_alert(env, capsys):
    cli.main(["--once", "--symbol", "TEST", "--strategy", "sma_rsi"])
    assert "sma_rsi" in capsys.readouterr().out


def test_backtest_single_symbol_uses_paper_order_sizing(env, capsys):
    assert cli.main(["--backtest", "--symbol", "TEST", "--notional", "250"]) == 0
    output = capsys.readouterr().out
    assert "sizing=notional notional=250" in output
    assert "backtest_run_id=" in output


def test_backtest_without_symbol_runs_every_watchlist_entry(env, tmp_path, capsys):
    (tmp_path / "watchlist.toml").write_text(
        '[[watch]]\nsymbol = "TEST"\n\n[[watch]]\nsymbol = "OTHER"\nnotional = 50\n',
        encoding="utf-8",
    )

    assert cli.main(["--backtest"]) == 0

    output = capsys.readouterr().out
    assert "TEST 1Day (stock)" in output and "OTHER 1Day (stock)" in output
    assert "total" in output
    import sqlite3

    with sqlite3.connect(tmp_path / "db.sqlite3") as connection:
        rows = connection.execute("SELECT symbol, sizing, notional FROM backtest_runs ORDER BY id").fetchall()
    assert rows == [("TEST", "notional", 100.0), ("OTHER", "notional", 50.0)]


def test_backtest_start_fetches_a_date_range(env, capsys):
    assert cli.main(["--backtest", "--symbol", "TEST", "--start", "2026-03-01", "--end", "2026-04-05"]) == 0
    (_, start, end), = env.range_requests
    assert start < datetime(2026, 3, 1, tzinfo=UTC)  # indicator warm-up before the start
    assert end == datetime(2026, 4, 6, tzinfo=UTC)  # a date-only end includes that day


def test_start_is_rejected_outside_backtests(env):
    with pytest.raises(SystemExit):
        cli.main(["--once", "--start", "2026-01-01"])
