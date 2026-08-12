"""行情查询必须覆盖数据仓中实际存在的全部品种。"""
import pandas as pd

from sinan.data.store import DataStore
from ui.quotes import market_catalog


def test_market_catalog_includes_stock_bars_missing_from_instruments(tmp_path):
    store = DataStore(tmp_path / "store")
    store.write_bars(pd.DataFrame({
        "symbol": ["000001"],
        "date": ["2024-01-02"],
        "open": [10.0],
        "high": [10.2],
        "low": [9.9],
        "close": [10.1],
    }), "stock")

    got = market_catalog(store).set_index("symbol")

    assert got.loc["000001", "sec_type"] == "stock"
    assert got.loc["000001", "name"] == ""
