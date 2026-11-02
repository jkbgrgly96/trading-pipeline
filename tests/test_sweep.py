from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from fakes import FakeBroker, random_walk_bars
from trading_pipeline.backtest.sweep import (
    CHECK,
    DEFAULT_GRIDS,
    TUNE,
    Period,
    Stats,
    SweepTarget,
    combinations,
    parse_grid,
    period_bars,
    result_rows,
    run_sweep,
    valid_combinations,
)
from trading_pipeline.domain import AssetClass

WALK_START = datetime(2022, 1, 1, tzinfo=UTC)


def d(year, month=1, day=1):
    return datetime(year, month, day, tzinfo=UTC)


PERIODS = [Period.of(d(2022, 6), d(2023), TUNE), Period.of(d(2023), d(2024), TUNE), Period.of(d(2024), None, CHECK)]


def test_period_names_follow_the_years():
    assert Period.of(d(2018), d(2021), TUNE).name == "2018-2020"
    assert Period.of(d(2024), None, CHECK).name == "2024-now"
    assert Period.of(d(2016), d(2017), TUNE).name == "2016"


def test_parse_grid_coerces_values_and_falls_back_to_the_default_grid():
    grid = parse_grid("indicathor", ["adx_min=0,20", "trend_filter=close,cross", "score_ema=3"])
    assert grid == {"adx_min": [0.0, 20.0], "trend_filter": ["close", "cross"], "score_ema": [3]}
    assert parse_grid("rsi2_pullback", None) == DEFAULT_GRIDS["rsi2_pullback"]


@pytest.mark.parametrize("bad", ["adx_min", "adx_min=", "nope=1", "score_ema=x"])
def test_parse_grid_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_grid("indicathor", [bad])


def test_invalid_combinations_are_skipped():
    grid = {"entry_length": [20, 55], "exit_length": [10, 20]}
    assert len(combinations(grid)) == 4
    valid = valid_combinations("donchian_breakout", {}, grid)
    assert {(p["entry_length"], p["exit_length"]) for p in valid} == {(20, 10), (55, 10), (55, 20)}


def test_stats_add_up_and_profit_factor_handles_no_losses():
    total = Stats(trades=2, wins=2, pnl=10, gross_profit=10).add(Stats(trades=1, pnl=-5, gross_loss=5))
    assert (total.trades, total.wins, total.pnl, total.profit_factor, total.expectancy) == (3, 2, 5, 2.0, 5 / 3)
    assert Stats(trades=1, wins=1, pnl=3, gross_profit=3).profit_factor == float("inf")
    assert Stats().metric("profit_factor") == float("-inf")


def targets() -> list[SweepTarget]:
    return [
        SweepTarget("AAA", AssetClass.STOCK, "1Day", random_walk_bars(1000, seed=3, start=WALK_START)),
        SweepTarget("BBB", AssetClass.STOCK, "1Day", random_walk_bars(1000, seed=5, start=WALK_START)),
    ]


def test_period_bars_include_warm_up_and_stop_at_the_period_end():
    target = targets()[0]
    bars = period_bars(target, PERIODS[1], min_bars=60)
    assert bars[-1].timestamp < d(2024)
    assert sum(1 for bar in bars if bar.timestamp < d(2023)) >= 60


def test_run_sweep_ranks_on_tune_periods_and_checks_finalists_on_every_period():
    pytest.importorskip("backtesting")
    grid = {"entry_length": [20, 55], "exit_length": [10]}

    outcome = run_sweep(targets(), "donchian_breakout", grid, PERIODS, top=1, min_trades=1, rank_by="pnl")

    tune = outcome.tune_periods
    pnls = [config.total(tune).pnl for config in outcome.ranked]
    consistent = [config.consistent(tune) for config in outcome.ranked]
    assert consistent == sorted(consistent, reverse=True)  # consistent configurations first
    for flag in (True, False):  # within each group, best P/L first
        group = [pnl for is_consistent, pnl in zip(consistent, pnls, strict=True) if is_consistent == flag]
        assert group == sorted(group, reverse=True)
    assert [c.label for c in outcome.reviewed] == ["#1", "defaults", "sma_rsi baseline"]
    for config in outcome.reviewed:
        assert set(config.results) == {"2022", "2023", "2024-now"}
    not_finalist = outcome.ranked[1]
    assert set(not_finalist.results) == {"2022", "2023"}  # never saw the check period
    assert outcome.reviewed[0].period_total("2024-now").trades > 0


def test_configurations_with_too_few_trades_are_not_finalists():
    pytest.importorskip("backtesting")
    outcome = run_sweep(targets(), "donchian_breakout", {"entry_length": [55]}, PERIODS, top=3, min_trades=10_000)
    assert outcome.finalists == []
    assert [c.label for c in outcome.reviewed] == ["defaults", "sma_rsi baseline"]


def test_a_symbol_without_data_in_a_period_is_recorded_not_fatal():
    pytest.importorskip("backtesting")
    late = SweepTarget("NEW", AssetClass.CRYPTO, "1Day", random_walk_bars(400, seed=9, start=d(2023, 6)))
    outcome = run_sweep([*targets(), late], "donchian_breakout", {"entry_length": [55]}, PERIODS, min_trades=1)
    assert outcome.defaults.results["2022"]["NEW 1Day"].errors == ["no data in this period"]
    rows = result_rows(outcome)
    assert {row["role"] for row in rows} == {TUNE, CHECK}
    assert any(row["symbol"] == "NEW" and row["error"] for row in rows)


def cli_env(tmp_path, monkeypatch):
    from trading_pipeline.brokers import alpaca

    monkeypatch.chdir(tmp_path)
    for key, value in {
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "DATABASE_PATH": str(tmp_path / "db.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    broker = FakeBroker(random_walk_bars(1000, seed=3, start=WALK_START))
    monkeypatch.setattr(alpaca, "AlpacaBroker", lambda **kwargs: broker)


def test_cli_sweep_stores_results_and_detail_backtests(tmp_path, monkeypatch, capsys):
    from trading_pipeline import cli

    cli_env(tmp_path, monkeypatch)
    code = cli.main([
        "--sweep", "--strategy", "donchian_breakout", "--symbol", "AAA",
        "--grid", "entry_length=20,55", "--grid", "exit_length=10",
        "--period", "2022-06-01:2022-12-31", "--period", "2023-01-01:2023-12-31", "--period", "2024-01-01:",
        "--min-trades", "1", "--top", "1",
    ])

    output = capsys.readouterr().out
    assert code == 0
    assert "Tune periods 2022, 2023" in output and "sma_rsi baseline" in output
    with sqlite3.connect(tmp_path / "db.sqlite3") as connection:
        (sweep_id, periods), = connection.execute("SELECT id, periods_json FROM sweeps").fetchall()
        roles = dict(connection.execute(
            "SELECT period, role FROM sweep_results WHERE sweep_id = ? GROUP BY period", (sweep_id,)
        ).fetchall())
        labels = [row[0] for row in connection.execute(
            "SELECT label FROM backtest_runs WHERE sweep_id = ? ORDER BY id", (sweep_id,)
        )]
    assert roles == {"2022": "tune", "2023": "tune", "2024-now": "check"}
    assert "2024-now" in periods
    assert labels == [f"sweep {sweep_id} #1 {p}" for p in ("2022", "2023", "2024-now")] + [
        f"sweep {sweep_id} defaults {p}" for p in ("2022", "2023", "2024-now")
    ]


def test_cli_sweep_without_periods_uses_start_split_end(tmp_path, monkeypatch, capsys):
    from trading_pipeline import cli

    cli_env(tmp_path, monkeypatch)
    assert cli.main([
        "--sweep", "--strategy", "donchian_breakout", "--symbol", "AAA", "--grid", "entry_length=55",
        "--start", "2022-06-01", "--split", "2024-01-01", "--min-trades", "1", "--detail-top", "0",
    ]) == 0
    assert "Tune periods 2022-2023" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["--sweep"],  # no strategy
        ["--sweep", "--strategy", "donchian_breakout", "--period", "2022-01-01"],
        ["--sweep", "--strategy", "donchian_breakout", "--period", "2022-01-01:2022-12-31", "--check-periods", "1"],
        ["--sweep", "--strategy", "donchian_breakout", "--period", "2022-01-01:2022-06-30",
         "--period", "2022-07-01:2022-12-31"],  # same year twice
    ],
)
def test_cli_sweep_rejects_bad_setups(tmp_path, monkeypatch, args):
    from trading_pipeline import cli

    cli_env(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(args)
