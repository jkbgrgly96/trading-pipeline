from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fakes import FakeBroker, random_walk_bars
from trading_pipeline.backtest.sweep import (
    DEFAULT_GRIDS,
    SweepTarget,
    combinations,
    parse_grid,
    run_sweep,
    valid_combinations,
)
from trading_pipeline.domain import AssetClass

WALK_START = datetime(2023, 1, 1, tzinfo=UTC)
SPLIT = datetime(2024, 1, 1, tzinfo=UTC)


def test_parse_grid_coerces_values_and_falls_back_to_the_default_grid():
    grid = parse_grid("indicathor", ["adx_min=0,20", "macd_flag_mode=state,cross", "score_ema=3"])
    assert grid == {"adx_min": [0.0, 20.0], "macd_flag_mode": ["state", "cross"], "score_ema": [3]}
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


def targets() -> list[SweepTarget]:
    return [
        SweepTarget("AAA", AssetClass.STOCK, "1Day", random_walk_bars(900, seed=3, start=WALK_START)),
        SweepTarget("BBB", AssetClass.STOCK, "1Day", random_walk_bars(900, seed=5, start=WALK_START)),
    ]


def test_run_sweep_ranks_in_sample_and_rechecks_finalists_out_of_sample():
    pytest.importorskip("backtesting")
    grid = {"entry_length": [20, 55], "exit_length": [10]}

    ranked, out_rows = run_sweep(
        targets(), "donchian_breakout", grid, start=datetime(2023, 4, 1, tzinfo=UTC),
        split=SPLIT, top=1, min_trades=1,
    )

    assert [row.in_sample.pnl for row in ranked] == sorted((row.in_sample.pnl for row in ranked), reverse=True)
    assert ranked[0].label == "#1"
    assert [row.label for row in out_rows] == ["#1", "defaults", "sma_rsi baseline"]
    assert out_rows[0].params == ranked[0].params
    assert all(row.out_of_sample is not None and row.out_of_sample.trades > 0 for row in out_rows[:2])
    assert out_rows[2].strategy == "sma_rsi"


def test_configurations_with_too_few_trades_are_not_finalists():
    pytest.importorskip("backtesting")
    ranked, out_rows = run_sweep(
        targets(), "donchian_breakout", {"entry_length": [55]}, start=datetime(2023, 4, 1, tzinfo=UTC),
        split=SPLIT, top=3, min_trades=10_000,
    )
    assert ranked[0].label.endswith("(few trades)")
    assert [row.label for row in out_rows] == ["defaults", "sma_rsi baseline"]


def test_cli_sweep_prints_both_periods_and_writes_a_csv(tmp_path, monkeypatch, capsys):
    from trading_pipeline import cli
    from trading_pipeline.brokers import alpaca

    monkeypatch.chdir(tmp_path)
    for key, value in {
        "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "DATABASE_PATH": str(tmp_path / "db.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    broker = FakeBroker(random_walk_bars(900, seed=3, start=WALK_START))
    monkeypatch.setattr(alpaca, "AlpacaBroker", lambda **kwargs: broker)

    code = cli.main([
        "--sweep", "--strategy", "donchian_breakout", "--symbol", "AAA",
        "--grid", "entry_length=20,55", "--grid", "exit_length=10",
        "--start", "2023-04-01", "--split", "2024-01-01", "--min-trades", "1", "--top", "1",
    ])

    output = capsys.readouterr().out
    assert code == 0
    assert "In-sample ranking" in output and "Out-of-sample 2024-01-01" in output
    assert "sma_rsi baseline" in output
    (csv_file,) = (tmp_path / "data" / "sweeps").glob("*_donchian_breakout.csv")
    assert "out_of_sample" in csv_file.read_text(encoding="utf-8")


def test_cli_sweep_needs_a_strategy(tmp_path, monkeypatch):
    from trading_pipeline import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(SystemExit):
        cli.main(["--sweep"])
