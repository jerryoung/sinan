"""双均线测试:金叉/死叉断言到具体 bar,防空验证。

合成序列的基底一律用微降漂移(而非纯横盘)——纯横盘会贴在指标边界
(ema_f == ema_s 恒等),仓库的 >/<= 约定会产生边界伪影而非规则验证。
EMA 用独立递推重算(不经 pandas ewm),交叉 bar 的判定与实现互为对照。
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sinan.config import load_rules, load_strategy
from sinan.signal.base import SignalContext, get_strategy
from sinan.signal.strategies.livermore import _atr
from sinan.signal.strategies.ma_cross import _replay_weight
from sinan.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]

P = dict(fast=20, slow=60, atr_n=20, atr_m=3.0, x_risk=0.025, cap=1.0)
WARM = max(P["slow"], P["atr_n"]) + 1                 # 预热起点 i = 61


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


def _ema(closes, span):
    """独立递推 EMA(等价 ewm(span, adjust=False)),与实现互为对照。"""
    alpha = 2.0 / (span + 1)
    out = [float(closes[0])]
    for c in closes[1:]:
        out.append(alpha * float(c) + (1 - alpha) * out[-1])
    return np.asarray(out)


def _tags(ev):
    return [t for _, t in ev]


_BASE = list(10.0 - 0.005 * np.arange(80))            # 微降漂移基底 10 → 9.605
_UP = list(9.7 + 0.15 * np.arange(1, 15))             # 上行段 9.85 → 11.8


# ---------------------------------------------------------------- 入场与定仓
def test_golden_cross_entry_bar_and_sizing():
    """金叉入场:bar 判定 = 首个 ema_f>ema_s 的有效 bar,权重手算校验。"""
    closes = _BASE + _UP
    df = _df(closes)
    ev = []
    w = _replay_weight(df, **P, events=ev)
    assert _tags(ev) == ["entry"]
    e = ev[0][0]
    ef, es = _ema(closes, P["fast"]), _ema(closes, P["slow"])
    expected = next(i for i in range(WARM, len(closes)) if ef[i] > es[i])
    assert e == expected                               # 交叉 bar 与独立递推一致
    assert e >= len(_BASE)                             # 金叉发生在上行段,不在基底
    entry_p = float(df["close"].iloc[e])
    n0 = float(_atr(df, P["atr_n"])[e - 1])            # 定仓用前一日 ATR
    assert w == pytest.approx(min(P["cap"], P["x_risk"] / (P["atr_m"] * n0 / entry_p)))
    fn = get_strategy("ma_cross")
    assert fn(_ctx(df), **P, lookback=750)["510300"] == pytest.approx(w)


def test_entry_on_first_valid_bar_if_already_crossed():
    """窗口起点即 ema_f>ema_s(单边上行)→ 首个有效 bar(WARM)入场。"""
    closes = list(10.0 + 0.01 * np.arange(120))
    ev = []
    w = _replay_weight(_df(closes), **P, events=ev)
    assert _tags(ev) == ["entry"]
    assert ev[0][0] == WARM
    assert w > 0


def test_no_entry_without_cross():
    """全程微降:ema_f 恒在 ema_s 下方,无事件、权重 0。"""
    ev = []
    drift = list(10.0 - 0.005 * np.arange(120))
    assert _replay_weight(_df(drift), **P, events=ev) == 0.0
    assert _tags(ev) == []


# ---------------------------------------------------------------- 出场归零
def test_death_cross_exit_bar_and_flat():
    """入场后转跌,首个 ema_f<=ema_s 的 bar 死叉清仓,权重归零。"""
    closes = _BASE + _UP + list(11.8 - 0.2 * np.arange(1, 25))   # 跌回 7.0
    df = _df(closes)
    ev = []
    w = _replay_weight(df, **P, events=ev)
    assert _tags(ev) == ["entry", "exit"]
    e, x = ev[0][0], ev[1][0]
    ef, es = _ema(closes, P["fast"]), _ema(closes, P["slow"])
    expected_x = next(i for i in range(e + 1, len(closes)) if ef[i] <= es[i])
    assert x == expected_x                             # 死叉 bar 与独立递推一致
    assert w == 0.0
    fn = get_strategy("ma_cross")
    assert fn(_ctx(df), **P, lookback=750) == {}       # 空仓不返回该标的


def test_reentry_after_exit():
    """死叉平仓后再度金叉 → 第二次入场,权重按新入场 bar 重新锁定。"""
    closes = (_BASE + _UP + list(11.8 - 0.2 * np.arange(1, 25))
              + list(7.0 + 0.25 * np.arange(1, 20)))             # 反转 → 11.75
    ev = []
    w = _replay_weight(_df(closes), **P, events=ev)
    assert _tags(ev) == ["entry", "exit", "entry"]
    assert w > 0


# ---------------------------------------------------------------- 无未来函数与配置
def test_no_lookahead():
    """today 截断:后接暴涨暴跌数据不改变截断日的输出。"""
    df = _df(_BASE + _UP)
    fn = get_strategy("ma_cross")
    w0 = fn(_ctx(df), **P, lookback=750).get("510300", 0.0)
    future = _df(list(df["close"]) + [15.0, 5.0, 20.0])
    w1 = fn(_ctx(future, today=df.index[-1]), **P, lookback=750).get("510300", 0.0)
    assert w0 == pytest.approx(w1) and w0 > 0


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "ma_cross_etf_h26.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == ("ma_cross_etf_h26", "ma_cross", "etf")
    assert cfg.lookback == 750
    assert cfg.universe == ["510300", "159915", "512010", "510420", "159995",
                            "512660", "512690", "513050", "512400", "515220",
                            "510900", "513500", "159941", "513030", "518880",
                            "159980", "159985", "511010", "515100", "159962",
                            "159518", "511090", "513130", "513520", "159611",
                            "513120"]
    assert cfg.params == {"fast": 20, "slow": 60, "atr_n": 20, "atr_m": 3.0,
                          "x_risk": 0.004375, "cap": 0.10}
