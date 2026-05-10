from trading_pipeline.indicators import rsi, sma


def test_sma_returns_none_until_window_is_full():
    assert sma([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]


def test_rsi_handles_all_gains():
    values = list(range(1, 20))
    output = rsi(values, 14)
    assert output[-1] == 100.0
