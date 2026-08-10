"""跳变阈值单一定义:拉取侧(入库前)与日更审计(入库后)同一口径。

背景:sina_feed 曾硬编码 JUMP_MAX=0.11。对涨跌幅 20% 的科创板 ETF
(588/589),一次合法的 15% 行情会被判成坏数据拒绝入库——而影子链路的
语义是"任一标的失败即中止,不出信号",于是一根正常 K 线就让当天没有
目标仓位。阈值必须按标的的 limit_pct 逐只算。
"""
import numpy as np
import pandas as pd
import pytest

from sinan.config import load_rules
from sinan.data.quality import JUMP_TOL, jump_threshold
from sinan.data.sina_feed import fetch_incremental
from sinan.data.store import DataStore


@pytest.fixture
def rules():
    return load_rules()


# ---------------------------------------------------------------- 阈值本身
def test_threshold_follows_limit_pct(rules):
    """10% 与 20% 两档品种拿到不同阈值——这正是全局常数做不到的。"""
    assert jump_threshold("510300", "etf", rules) == pytest.approx(0.10 * JUMP_TOL)
    assert jump_threshold("588000", "etf", rules) == pytest.approx(0.20 * JUMP_TOL)


def test_star_etf_threshold_admits_legal_move(rules):
    """科创板 ETF 的 15% 合法行情:旧的 0.11 会误杀,新阈值放行。"""
    thr = jump_threshold("588000", "etf", rules)
    assert 0.15 > 0.11, "前提:旧硬编码阈值确实低于该行情"
    assert 0.15 <= thr, "科创板 ETF 的 15% 波动必须被放行"


def test_threshold_loads_rules_when_omitted():
    assert jump_threshold("510300", "etf") == pytest.approx(0.10 * JUMP_TOL)


# ---------------------------------------------------------------- 拉取侧接线
def _seed(store, symbol, close, sec_type="etf"):
    """给 store 播一根昨日 K 线,作为增量拉取的前收盘基准。"""
    store.write_bars(pd.DataFrame([{
        "symbol": symbol, "date": pd.Timestamp("2026-08-06"),
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1e6, "amount": close * 1e6, "adj_factor": 1.0,
    }]), sec_type)


def _fake_ak(monkeypatch, symbol, close):
    """替身数据源:返回一根指定收盘价的新 K 线(不联网)。"""
    import akshare as ak
    row = pd.DataFrame([{
        "date": "2026-08-07", "open": close, "high": close,
        "low": close, "close": close, "volume": 1e6,
    }])
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol=None: row.copy())


@pytest.mark.parametrize("symbol,new_close,expect_ok", [
    # 科创板 ETF(20% 限制):+15% 合法 —— 旧的 0.11 阈值会误杀
    ("588000", 11.5, True),
    # 科创板 ETF:+25% 超出 20%×1.05,仍应拦截(没有放松成"不检查")
    ("588000", 12.5, False),
    # 宽基 ETF(10% 限制):+15% 不合法,必须拦截
    ("510300", 11.5, False),
])
def test_fetch_incremental_uses_per_symbol_threshold(
        tmp_path, monkeypatch, symbol, new_close, expect_ok):
    store = DataStore(tmp_path / "store")
    _seed(store, symbol, 10.0)
    _fake_ak(monkeypatch, symbol, new_close)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    res = fetch_incremental(store, [symbol], sec_type="etf", pause=0, log=lambda *_: None)
    if expect_ok:
        assert res["updated"] == [symbol], f"合法行情被误杀: {res['failed']}"
        assert not res["failed"]
        got = store.read_bars(symbols=[symbol], sec_type="etf")
        assert float(got["close"].iloc[-1]) == pytest.approx(new_close)
    else:
        assert res["updated"] == []
        assert res["failed"] and "跳变" in res["failed"][0]["error"]
        # 拦截语义 = 不入库:坏 K 线不能进数据仓
        got = store.read_bars(symbols=[symbol], sec_type="etf")
        assert len(got) == 1 and float(got["close"].iloc[-1]) == pytest.approx(10.0)


def test_fetch_incremental_still_blocks_invalid_ohlc(tmp_path, monkeypatch):
    """OHLC 硬门槛未被这次改动放松。"""
    import akshare as ak
    store = DataStore(tmp_path / "store")
    _seed(store, "510300", 10.0)
    bad = pd.DataFrame([{"date": "2026-08-07", "open": 10.0, "high": 9.0,
                         "low": 10.5, "close": 10.1, "volume": 1e6}])
    monkeypatch.setattr(ak, "fund_etf_hist_sina", lambda symbol=None: bad.copy())
    monkeypatch.setattr("time.sleep", lambda *_: None)

    res = fetch_incremental(store, ["510300"], sec_type="etf", pause=0, log=lambda *_: None)
    assert res["updated"] == []
    assert "OHLC" in res["failed"][0]["error"]


def test_ensure_keeps_its_own_lenient_policy():
    """全量补数的宽松告警阈值是有意为之的第三种口径,不该被"统一"掉。"""
    from sinan.data import ensure
    assert ensure._JUMP_WARN == 0.20
    assert not hasattr(ensure, "_JUMP_BLOCK"), "全量历史跳变只告警,不拦截"
    assert np.isfinite(ensure._JUMP_WARN)
