"""ensure_bars 按需补数:已有标的跳过、缺失标的拉全量入库、拉不到的如实上报。

数据源用桩模块注入(sys.modules["akshare"]),不触网。
"""
import sys
import types

import pandas as pd
import pytest

from sinan.data.ensure import ensure_bars
from sinan.data.store import DataStore

DATES = pd.date_range("2024-01-01", periods=5, freq="B")


def _seed(tmp_path, symbol="510001"):
    store = DataStore(tmp_path / "store")
    df = pd.DataFrame({
        "symbol": symbol, "date": DATES,
        "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
        "volume": 1e6, "amount": 1.05e6, "adj_factor": 1.0,
    })
    store.write_bars(df, "etf")
    return store


def _stub_akshare(monkeypatch, ok_symbols):
    """桩:ok_symbols 内的代码返回合成日线,其余抛异常(模拟拉取失败)。"""
    mod = types.ModuleType("akshare")

    def fund_etf_hist_sina(symbol):
        code = symbol[2:]
        if code not in ok_symbols:
            raise RuntimeError("connection refused")
        return pd.DataFrame({
            "date": DATES.strftime("%Y-%m-%d"),
            "open": 2.0, "high": 2.2, "low": 1.9, "close": 2.1, "volume": 500,
        })

    mod.fund_etf_hist_sina = fund_etf_hist_sina
    monkeypatch.setitem(sys.modules, "akshare", mod)


def test_all_present_no_fetch(tmp_path, monkeypatch):
    store = _seed(tmp_path)
    _stub_akshare(monkeypatch, ok_symbols=set())      # 若触网必失败
    assert ensure_bars(store, ["510001"], "etf", log=lambda *_: None) == []


def test_fetch_missing_and_report_failures(tmp_path, monkeypatch):
    store = _seed(tmp_path)
    _stub_akshare(monkeypatch, ok_symbols={"510999"})
    msgs = []
    still = ensure_bars(store, ["510001", "510999", "512000"], "etf",
                        log=msgs.append)
    assert still == ["512000"]                        # 拉不到的如实上报
    got = store.read_bars(symbols=["510999"], sec_type="etf")
    assert len(got) == len(DATES)                     # 拉到的已入库
    assert float(got["adj_factor"].iloc[-1]) == 1.0   # 未复权起步因子
    assert any("补数 510999" in m for m in msgs)
    assert any("补数失败 512000" in m for m in msgs)
    # 第二次调用:510999 已入库不再拉取,只剩 512000
    assert ensure_bars(store, ["510001", "510999", "512000"], "etf",
                       log=msgs.append) == ["512000"]


def test_non_etf_not_supported(tmp_path, monkeypatch):
    store = _seed(tmp_path)
    _stub_akshare(monkeypatch, ok_symbols={"113050"})
    still = ensure_bars(store, ["113050"], "cb", log=lambda *_: None)
    assert still == ["113050"]                        # 非 ETF 不自动补数
