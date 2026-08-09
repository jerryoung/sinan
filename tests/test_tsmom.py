"""TSMOM(12-1)测试:入场定仓/动量出场/skip 语义/无未来函数/YAML,防空验证。

合成序列的基底一律用微降漂移(而非纯横盘)——纯横盘会贴指标边界
(mom 恰为 0、close 贴通道),>=/<= 约定下产生边界伪影而非规则验证。
状态机测试用缩尺参数(mom_n=40, skip_n=5),规则逻辑与生产参数
(244/21)完全同构;生产参数在 YAML 断言中锁定。
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.config import load_rules, load_strategy
from trend.signal.base import SignalContext, get_strategy
from trend.signal.strategies.livermore import _atr
from trend.signal.strategies.tsmom import _replay_weight
from trend.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]

P = dict(mom_n=40, skip_n=5, atr_n=10, atr_m=3.0, x_risk=0.025, cap=1.0)


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


def _tags(ev):
    return [t for _, t in ev]


_BASE = list(10.0 - 0.005 * np.arange(60))          # 微降漂移基底 10 → 9.705
_UP = list(9.75 + 0.05 * np.arange(1, 26))          # 上行段 9.80 → 11.00


def _first_signal(closes):
    """独立复算首个 mom>0 的 bar 下标(不经状态机),防状态机自证。"""
    c = np.asarray(closes, dtype=float)
    idx = np.arange(P["mom_n"], len(c))
    mask = c[idx - P["skip_n"]] / c[idx - P["mom_n"]] - 1.0 > 0
    assert mask.any()
    return int(idx[np.argmax(mask)])


# ---------------------------------------------------------------- 入场与定仓
def test_entry_and_sizing():
    """首个 mom>0 的 bar 入场,权重 = x_risk/(atr_m·ATR[e-1]/入场价),锁定。"""
    df = _df(_BASE + _UP)
    ev = []
    w = _replay_weight(df, **P, events=ev)
    assert _tags(ev) == ["entry"]                   # 全程仅一次入场,无出场
    e = ev[0][0]
    assert e == _first_signal(df["close"])          # 入场恰在首个动量转正 bar
    assert e > len(_BASE)                           # 且发生在上行段,不在基底
    entry_p = float(df["close"].iloc[e])
    n0 = float(_atr(df, P["atr_n"])[e - 1])
    assert n0 == pytest.approx(0.2)                 # spread=0.1 ⇒ TR 恒为 0.2
    assert w == pytest.approx(min(1.0, P["x_risk"] / (P["atr_m"] * n0 / entry_p)))
    assert w == pytest.approx(P["x_risk"] * entry_p / (P["atr_m"] * 0.2))  # 手算同值
    fn = get_strategy("tsmom")
    assert fn(_ctx(df), **P, lookback=750)["510300"] == pytest.approx(w)


def test_no_entry_without_positive_momentum():
    """全程微降 ⇒ close[i-5] < close[i-40] 恒成立,mom 恒负,永不入场。"""
    ev = []
    drift90 = list(10.0 - 0.005 * np.arange(90))
    assert _replay_weight(_df(drift90), **P, events=ev) == 0.0
    assert _tags(ev) == []


def test_insufficient_history_excluded():
    """历史不足 mom_n+1 根:策略层直接不参与(生产参数 244 需一年史)。"""
    df = _df(list(10.0 - 0.005 * np.arange(100)))
    fn = get_strategy("tsmom")
    assert fn(_ctx(df)) == {}                       # 默认 mom_n=244 > 100


# ---------------------------------------------------------------- 出场归零
def test_exit_when_momentum_turns_negative():
    """暴跌持续超过 skip_n 根后进入动量窗口 ⇒ mom 翻负 ⇒ 清仓归零。"""
    closes = _BASE + _UP
    assert _replay_weight(_df(closes), **P) > 0     # 前置:确在持仓
    crash = list(5.0 - 0.005 * np.arange(P["skip_n"] + 1))   # 6 根 < 入场价一半
    ev = []
    w = _replay_weight(_df(closes + crash), **P, events=ev)
    assert w == 0.0
    tags = _tags(ev)
    assert tags == ["entry", "exit"]                # 一进一出,无重复触发
    assert ev[-1][0] == len(closes) + P["skip_n"]   # 暴跌满 skip_n+1 根才生效


# ---------------------------------------------------------------- skip 语义
def test_skip_recent_crash_does_not_change_signal():
    """12-1 精髓:最近 skip_n 内暴跌不改变信号——动量用 skip_n 之前的价。

    追加 skip_n−1 根腰斩暴跌(任何价格止损都会触发的量级),但这些 bar
    尚未进入 close[i-skip_n] 窗口 ⇒ mom 仍为正 ⇒ 继续持有,权重不变。
    """
    closes = _BASE + _UP
    ev0 = []
    w0 = _replay_weight(_df(closes), **P, events=ev0)
    assert w0 > 0
    crash = list(5.0 - 0.005 * np.arange(P["skip_n"] - 1))   # 4 根 < skip_n
    ev = []
    w1 = _replay_weight(_df(closes + crash), **P, events=ev)
    assert w1 == pytest.approx(w0)                  # 权重原样(入场锁定且未出场)
    assert _tags(ev) == ["entry"]                   # 无 exit:信号未受近期暴跌影响
    assert ev[0][0] == ev0[0][0]                    # 入场 bar 也不变


# ---------------------------------------------------------------- 无未来函数与配置
def test_no_lookahead():
    """today 截断:后接暴涨暴跌数据不改变截断日的输出。"""
    df = _df(_BASE + _UP)
    fn = get_strategy("tsmom")
    w0 = fn(_ctx(df), **P, lookback=750).get("510300", 0.0)
    future = _df(list(df["close"]) + [15.0, 5.0, 20.0])
    w1 = fn(_ctx(future, today=df.index[-1]), **P, lookback=750).get("510300", 0.0)
    assert w0 == pytest.approx(w1) and w0 > 0


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "tsmom_etf_h26.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == ("tsmom_etf_h26", "tsmom", "etf")
    assert cfg.lookback == 750
    assert cfg.universe == ["510300", "159915", "512010", "510420", "159995",
                            "512660", "512690", "513050", "512400", "515220",
                            "510900", "513500", "159941", "513030", "518880",
                            "159980", "159985", "511010", "515100", "159962",
                            "159518", "511090", "513130", "513520", "159611",
                            "513120"]
    assert cfg.params == {"mom_n": 244, "skip_n": 21, "atr_n": 20, "atr_m": 3.0,
                          "x_risk": 0.004375, "cap": 0.10}
