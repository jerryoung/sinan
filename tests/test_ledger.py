"""策略账本:实盘 fills 序列、影子 targets 重放、绩效统计口径。"""
import json

import numpy as np
import pandas as pd
import pytest

from sinan.live import ledger
from sinan.data.store import DataStore


# ---------------- 实盘序列 ----------------
def _write_fill(d, strategy, date, total, orders=None):
    (d / f"fills_{strategy}_{date.replace('-', '')}.json").write_text(
        json.dumps({"date": date, "strategy": strategy, "trade_mode": "sim",
                    "total_asset": total, "cash": 0.0, "weights": {},
                    "fills": orders or [], "positions": {}}), encoding="utf-8")


def test_live_nav_and_trades(tmp_path):
    _write_fill(tmp_path, "s1", "2024-01-02", 100000.0,
                [{"symbol": "510300", "side": "buy", "qty": 100, "price": 4.0}])
    _write_fill(tmp_path, "s1", "2024-01-03", 101000.0)
    _write_fill(tmp_path, "other", "2024-01-04", 999999.0)   # 别的策略不串账
    fills = ledger.load_fills(tmp_path, "s1")
    assert [f["date"] for f in fills] == ["2024-01-02", "2024-01-03"]
    nav = ledger.live_nav(fills)
    assert nav.iloc[0] == 1.0
    assert nav.iloc[-1] == pytest.approx(1.01)
    tr = ledger.live_trades(fills)
    assert len(tr) == 1 and tr.iloc[0]["symbol"] == "510300"


# ---------------- 影子重放 ----------------
def test_shadow_nav_piecewise(tmp_path):
    store = DataStore(tmp_path / "store")
    dates = pd.date_range("2024-01-01", periods=6, freq="B")
    px = [10.0, 10.0, 11.0, 11.0, 11.0, 12.1]      # 日收益 0,10%,0,0,10%
    store.write_bars(pd.DataFrame({
        "symbol": "510001", "date": dates, "open": px, "high": px, "low": px,
        "close": px, "volume": 1e6, "amount": 1e7, "adj_factor": 1.0}), "etf")
    tdir = tmp_path / "targets"
    tdir.mkdir()
    # 第 1 天满仓 50%,第 4 天调到 100%
    for date, w in [(dates[0], 0.5), (dates[3], 1.0)]:
        ymd = date.strftime("%Y%m%d")
        (tdir / f"targets_s1_{ymd}.json").write_text(json.dumps({
            "date": str(date.date()), "strategy": "s1",
            "targets": {"510001": w}}), encoding="utf-8")
    nav = ledger.shadow_nav(store, tdir, "s1")
    # 权重执行日收盘生效:d2 涨 10% 吃 50%,d6 涨 10% 吃 100%
    assert nav.iloc[-1] == pytest.approx(1.05 * 1.10, rel=1e-9)
    assert nav.iloc[0] == 1.0


def test_shadow_nav_empty(tmp_path):
    store = DataStore(tmp_path / "store")
    assert ledger.shadow_nav(store, tmp_path / "nope", "s1").empty


# ---------------- 绩效统计 ----------------
def test_perf_stats_windows_and_mdd():
    idx = pd.bdate_range("2023-01-02", periods=400)
    rng = np.random.default_rng(7)
    nav = pd.Series((1 + pd.Series(rng.normal(0.0005, 0.01, 400))).cumprod().values,
                    index=idx)
    s = ledger.perf_stats(nav)
    assert s["cum"] == pytest.approx(float(nav.iloc[-1] / nav.iloc[0] - 1))
    assert s["daily"] == pytest.approx(float(nav.iloc[-1] / nav.iloc[-2] - 1))
    # 回撤区间自洽:峰在谷前,谷值 = mdd
    dd = nav / nav.cummax() - 1
    assert s["mdd"] == pytest.approx(float(dd.min()))
    assert pd.Timestamp(s["mdd_start"]) <= pd.Timestamp(s["mdd_end"])
    # 一年窗口存在(400 交易日 > 1 自然年)
    assert s["y1"] is not None
    # 短序列:不足回看窗口返回 None 而非外推
    s2 = ledger.perf_stats(nav.iloc[:10])
    assert s2["m1"] is None and s2["y1"] is None


def test_perf_stats_too_short():
    assert ledger.perf_stats(pd.Series([1.0])) == {}
