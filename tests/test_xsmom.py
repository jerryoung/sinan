"""横截面动量测试:排名/top_k 截断、绝对动量过滤、逐日定仓手算、无未来函数。

多标的合成 ctx;状态逻辑用缩小参数(mom_n=40, skip_n=5, atr_n=10)保证
序列可手算,生产参数由 YAML 断言锁定。基底一律微降漂移(仓库约定)。
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.config import load_rules, load_strategy
from trend.signal.base import SignalContext, get_strategy
from trend.signal.strategies.livermore import _atr
from trend.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]

P = dict(mom_n=40, skip_n=5, top_k=2, atr_n=10, atr_m=3.0,
         x_risk=0.025, cap=1.0)


def _df(closes, spread=0.1):
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range("2020-01-01", periods=len(c), freq="B")
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread,
                         "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)


def _ctx(frames, today=None):
    rules_cfg = load_rules()
    rules = {s: resolve_rule(s, "etf", rules_cfg) for s in frames}
    any_idx = next(iter(frames.values())).index
    return SignalContext(today=today or any_idx[-1], data=frames,
                         positions={}, total_asset=1e6, rules=rules,
                         universe=list(frames), cb_terms=None)


def _ramp(total_ret, n=60, base=10.0):
    """60 根、首尾涨幅为 total_ret 的等差序列(动量可控)。"""
    return list(np.linspace(base, base * (1 + total_ret), n))


def test_ranking_and_topk():
    """已知动量 A>B>0>C:top_k=2 → 只持 A、B;C 被绝对动量过滤。"""
    frames = {"510300": _df(_ramp(0.40)),      # 最强
              "510500": _df(_ramp(0.15)),      # 次强
              "518880": _df(_ramp(-0.20))}     # 下跌 → mom<0
    w = get_strategy("xsmom")(_ctx(frames), **P, lookback=750)
    assert set(w) == {"510300", "510500"}

    w1 = get_strategy("xsmom")(_ctx(frames), **dict(P, top_k=1), lookback=750)
    assert set(w1) == {"510300"}               # top_k 截断保留动量最高者


def test_negative_mom_all_flat():
    """全池 mom<=0 → 空仓(不选矮子里的将军)。"""
    frames = {"510300": _df(_ramp(-0.10)), "510500": _df(_ramp(-0.30))}
    assert get_strategy("xsmom")(_ctx(frames), **P, lookback=750) == {}


def test_sizing_hand_check():
    """权重 = min(cap, x_risk/(atr_m×ATR末值/close末值)),逐日口径。"""
    frames = {"510300": _df(_ramp(0.40))}
    df = frames["510300"]
    a, p = _atr(df, P["atr_n"])[-1], float(df["close"].iloc[-1])
    expect = min(P["cap"], P["x_risk"] / (P["atr_m"] * a / p))
    w = get_strategy("xsmom")(_ctx(frames), **P, lookback=750)
    assert w["510300"] == pytest.approx(expect)


def test_insufficient_history_excluded():
    """bars < mom_n+2 的标的不参与排名(即使涨得最猛)。"""
    frames = {"510300": _df(_ramp(0.10)),
              "159915": _df(_ramp(0.90, n=30))}     # 仅 30 根 < 42
    w = get_strategy("xsmom")(_ctx(frames), **P, lookback=750)
    assert set(w) == {"510300"}


def test_skip_semantics():
    """动量用 skip_n 之前的价:最近 skip_n−1 根暴跌不改变排名输入。"""
    base = _ramp(0.40)
    crashed = base[:-4] + [base[-5] * 0.7] * 4      # 最近 4 根(< skip_n=5)暴跌
    w0 = get_strategy("xsmom")(_ctx({"510300": _df(base)}), **P, lookback=750)
    w1 = get_strategy("xsmom")(_ctx({"510300": _df(crashed)}), **P, lookback=750)
    # 仍在场(动量不看最近 skip 窗口);权重因 ATR 变化不同,不比数值
    assert "510300" in w0 and "510300" in w1


def test_no_lookahead():
    """today 截断:未来数据不影响当日输出。"""
    frames = {"510300": _df(_ramp(0.40)), "510500": _df(_ramp(0.15))}
    today = frames["510300"].index[-1]
    w0 = get_strategy("xsmom")(_ctx(frames), **P, lookback=750)
    ext = {s: _df(list(f["close"]) + [50.0, 1.0, 80.0]) for s, f in frames.items()}
    w1 = get_strategy("xsmom")(_ctx(ext, today=today), **P, lookback=750)
    assert w0 == pytest.approx(w1)


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "xsmom_etf_h26.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == ("xsmom_etf_h26", "xsmom", "etf")
    assert cfg.lookback == 750 and len(cfg.universe) == 26
    assert cfg.params == {"mom_n": 244, "skip_n": 21, "top_k": 5, "atr_n": 20,
                          "atr_m": 3.0, "x_risk": 0.004375, "cap": 0.10}
