from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from fakes import random_walk_bars
from trading_pipeline.backtest.sweep import CHECK, TUNE, Period, SweepTarget, result_rows, run_sweep
from trading_pipeline.dashboard import queries
from trading_pipeline.dashboard.charts import build_pnl_heatmap
from trading_pipeline.domain import AssetClass
from trading_pipeline.storage import Storage

START = datetime(2022, 1, 1, tzinfo=UTC)
PERIODS = [
    Period.of(datetime(2022, 6, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC), TUNE),
    Period.of(datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC), TUNE),
    Period.of(datetime(2024, 1, 1, tzinfo=UTC), None, CHECK),
]


@pytest.fixture
def stored_sweep(tmp_path):
    pytest.importorskip("backtesting")
    storage = Storage(tmp_path / "db.sqlite3")
    storage.initialize()
    targets = [
        SweepTarget("AAA", AssetClass.STOCK, "1Day", random_walk_bars(1000, seed=3, start=START)),
        SweepTarget("BBB", AssetClass.STOCK, "1Day", random_walk_bars(1000, seed=5, start=START)),
    ]
    grid = {"entry_length": [20, 55], "exit_length": [10]}
    outcome = run_sweep(targets, "donchian_breakout", grid, PERIODS, top=1, min_trades=1)
    sweep_id = storage.save_sweep(
        "donchian_breakout", grid, {}, [p.as_dict() for p in PERIODS], "profit_factor", 1, ["AAA 1Day", "BBB 1Day"]
    )
    storage.save_sweep_results(sweep_id, result_rows(outcome))
    return tmp_path / "db.sqlite3", sweep_id, outcome


def test_sweeps_load_and_summarize_per_configuration(stored_sweep):
    path, sweep_id, outcome = stored_sweep

    sweeps = queries.load_sweeps(path)
    results = queries.load_sweep_results(path, sweep_id)
    table = queries.sweep_config_table(results, [p.name for p in PERIODS])

    assert sweeps["id"].tolist() == [sweep_id]
    assert table["label"].tolist() == ["#1", "#2", "defaults", "sma_rsi baseline"]
    assert table["reviewed"].tolist() == [True, False, True, True]
    best = table.iloc[0]
    finalist = outcome.finalists[0]
    assert best["tune trades"] == finalist.total(outcome.tune_periods).trades
    assert best["pnl 2024-now"] == pytest.approx(finalist.period_total("2024-now").pnl)
    assert best["consistent"] == finalist.consistent(outcome.tune_periods)
    assert math.isnan(table.iloc[1]["pnl 2024-now"])  # never ran on the check period


def test_summarize_sweep_profit_factor_without_losses_is_infinite():
    import pandas as pd

    rows = pd.DataFrame([
        {"config": 1, "trades": 2, "wins": 2, "pnl": 10.0, "gross_profit": 10.0, "gross_loss": 0.0, "return_pct_sum": 4.0},
        {"config": 2, "trades": 0, "wins": 0, "pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "return_pct_sum": 0.0},
    ])
    summary = queries.summarize_sweep(rows, ["config"])
    assert summary["profit_factor"].iloc[0] == float("inf")
    assert math.isnan(summary["profit_factor"].iloc[1])
    assert summary["pnl_per_trade"].iloc[0] == 5.0


def test_sweeps_are_empty_on_a_database_without_the_tables(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    sqlite3.connect(path).close()
    assert queries.load_sweeps(path).empty


def test_pnl_heatmap_is_diverging_around_zero_and_leaves_gaps_empty(stored_sweep):
    path, sweep_id, _ = stored_sweep
    results = queries.load_sweep_results(path, sweep_id)
    per_symbol = queries.summarize_sweep(results[results["config"] == 0], ["symbol", "period"])

    figure = build_pnl_heatmap(per_symbol, "symbol", ["AAA", "BBB", "MISSING"], [p.name for p in PERIODS], {"2024-now"})

    heatmap = figure.data[0]
    assert list(heatmap.x) == ["2022", "2023", "2024-now (check)"]
    assert heatmap.zmid == 0 and heatmap.zmin == -heatmap.zmax
    assert list(heatmap.z[2]) == [None, None, None]
    assert heatmap.text[2][0] == "no trades"


def test_sweeps_tab_renders(stored_sweep, monkeypatch):
    from streamlit.testing.v1 import AppTest

    path, sweep_id, _ = stored_sweep
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    app = AppTest.from_file("src/trading_pipeline/dashboard/app.py", default_timeout=60)
    app.run()

    assert not app.exception
    assert any(header.value == "Finalists, defaults and sma_rsi in every period" for header in app.subheader)
    assert any(f"#{sweep_id} donchian_breakout" in str(box.options) for box in app.selectbox)


def test_sweeps_tab_renders_next_to_the_other_tabs(stored_sweep, monkeypatch):
    from streamlit.testing.v1 import AppTest

    path, sweep_id, _ = stored_sweep
    Storage(path).save_market_bars("AAA", AssetClass.STOCK, "1Day", random_walk_bars(30, start=START))
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    app = AppTest.from_file("src/trading_pipeline/dashboard/app.py", default_timeout=60)
    app.run()

    assert not app.exception
    assert [tab.label for tab in app.tabs][-1] == "Sweeps"
    assert any(header.value == "Per symbol" for header in app.subheader)
