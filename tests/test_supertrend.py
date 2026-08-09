"""SuperTrend 测试:翻多/翻空事件断言、棘轮下轨、定仓手算、无未来函数。"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.config import load_rules, load_strategy
from trend.signal.base import SignalContext, get_strategy
from trend.signal.strategies.livermore import _atr
from trend.signal.strategies.supertrend import _replay_weight
from trend.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]
P = dict(atr_n=10, mult=3.0, x_risk=0.025, cap=1.0)


def _df(closes, spread=0.1):
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range("2020-01-01", periods=len(c), freq="B")
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread,
                         "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)


def _ctx(df, today=None, sym="510300"):
    rules = {sym: resolve_rule(sym, "etf", load_rules())}
    return SignalContext(today=today or df.index[-1], data={sym: df},
                         positions={}, total_asset=1e6, rules=rules,
                         universe=[sym], cb_terms=None)


_BASE = list(10.0 - 0.005 * np.arange(40))          # 微降漂移基底(仓库约定)


def test_flip_up_entry_and_sizing():
    """上穿上轨翻多:事件断言到 flip_up,权重 = x_risk/(mult×ATR/close)。"""
    closes = _BASE + list(9.8 + 0.35 * np.arange(1, 15))   # 强上行穿上轨
    df = _df(closes)
    ev = []
    w = _replay_weight(df, **P, events=ev)
    tags = [t for _, t in ev]
    assert tags.count("flip_up") >= 1 and w > 0
    e = next(i for i, t in ev if t == "flip_up")
    a = _atr(df, P["atr_n"])[e]
    expect = min(P["cap"], P["x_risk"] / (P["mult"] * a / df["close"].iloc[e]))
    assert w == pytest.approx(expect)               # 入场 bar 锁定,持有期不变
    fn = get_strategy("supertrend")
    assert fn(_ctx(df), **P, lookback=750)["510300"] == pytest.approx(w)


def test_no_entry_on_drift():
    ev = []
    assert _replay_weight(_df(list(10 - 0.005 * np.arange(60))), **P, events=ev) == 0.0
    assert ev == []


def test_flip_down_exit():
    """跌破棘轮下轨翻空清仓(事件断言 flip_down),策略层返回 {}。"""
    closes = _BASE + list(9.8 + 0.35 * np.arange(1, 15))
    df0 = _df(closes)
    ev0 = []
    assert _replay_weight(df0, **P, events=ev0) > 0        # 前置:持仓中
    # 跌破下轨:入场后高点 ~14.7,ATR≈0.4 → 下轨 ≈ hl2−1.2 的棘轮高位
    crash = list(df0["close"].iloc[-1] - 0.8 * np.arange(1, 5))
    ev = []
    w = _replay_weight(_df(closes + crash), **P, events=ev)
    tags = [t for _, t in ev]
    assert w == 0.0 and "flip_down" in tags
    assert tags.index("flip_down") > tags.index("flip_up")
    fn = get_strategy("supertrend")
    assert fn(_ctx(_df(closes + crash)), **P, lookback=750) == {}


def test_ratchet_lower_band():
    """棘轮:上行期间下轨只升不降——浅回调(< mult×ATR)不出场。"""
    closes = _BASE + list(9.8 + 0.35 * np.arange(1, 15))
    dip = [14.5, 14.3, 14.5]                        # 浅回调,远高于下轨
    ev = []
    w = _replay_weight(_df(closes + dip), **P, events=ev)
    assert w > 0 and "flip_down" not in [t for _, t in ev]


def test_no_lookahead():
    closes = _BASE + list(9.8 + 0.35 * np.arange(1, 15))
    df = _df(closes)
    fn = get_strategy("supertrend")
    w0 = fn(_ctx(df), **P, lookback=750).get("510300", 0.0)
    ext = _df(closes + [30.0, 3.0, 50.0])
    w1 = fn(_ctx(ext, today=df.index[-1]), **P, lookback=750).get("510300", 0.0)
    assert w0 == pytest.approx(w1) and w0 > 0


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "supertrend_etf_h26.yaml")
    assert (cfg.name, cfg.strategy) == ("supertrend_etf_h26", "supertrend")
    assert len(cfg.universe) == 26 and cfg.lookback == 750
    assert cfg.params == {"atr_n": 10, "mult": 3.0, "x_risk": 0.004375, "cap": 0.10}
