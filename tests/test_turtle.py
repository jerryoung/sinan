"""海龟 S1 测试:事件断言到具体规则分支(过滤器/兜底/两种出场),防空验证。

合成序列的基底一律用微降漂移(而非纯横盘)——纯横盘会恰好贴在通道
边界上(close == 20日最高 == 10日最低),仓库的 >=/<= 突破约定会让
入场出场同 bar 连环触发,测试变成边界伪影而非规则验证。
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sinan.config import load_rules, load_strategy
from sinan.signal.base import SignalContext, get_strategy
from sinan.signal.strategies.livermore import _atr
from sinan.signal.strategies.turtle import _replay_weight
from sinan.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]

P = dict(n_entry=20, n_exit=10, n_failsafe=55, atr_n=20,
         stop_n_mult=2.0, x_risk=0.025, cap=1.0)


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


_BASE = list(10.0 - 0.005 * np.arange(60))        # 微降漂移基底 10 → 9.705


# ---------------------------------------------------------------- 入场与定仓
def test_entry20_and_sizing():
    """20 日突破 → entry20,权重 = x_risk/(2N/入场价),入场锁定。"""
    df = _df(_BASE + [10.1, 10.4])
    ev = []
    w = _replay_weight(df, **P, events=ev)
    assert _tags(ev) == ["entry20"]
    e = next(i for i, t in ev if t == "entry20")
    assert e == 60                                  # 突破发生在跳升那根,不在基底
    entry_p = float(df["close"].iloc[e])
    n0 = float(_atr(df, P["atr_n"])[e - 1])
    assert w == pytest.approx(min(1.0, P["x_risk"] / (2.0 * n0 / entry_p)))
    fn = get_strategy("turtle_s1")
    assert fn(_ctx(df), **P, lookback=750)["510300"] == pytest.approx(w)


def test_no_entry_without_breakout():
    ev = []
    drift80 = list(10.0 - 0.005 * np.arange(80))    # 全程微降,永无突破
    assert _replay_weight(_df(drift80), **P, events=ev) == 0.0
    assert _tags(ev) == []                          # 无突破则全程无事件


# ---------------------------------------------------------------- 两种出场
def test_exit10_reverse_breakout():
    """入场后上行,收盘跌破前一日 10 日最低 → exit10 全平。"""
    closes = _BASE + list(9.8 + 0.3 * np.arange(1, 13))
    df = _df(closes)
    ev0 = []
    assert _replay_weight(df, **P, events=ev0) > 0            # 前置:确在持仓
    e = next(i for i, t in ev0 if t == "entry20")
    entry_p = float(df["close"].iloc[e])
    n0 = float(_atr(df, P["atr_n"])[e - 1])
    le10 = float(pd.Series(df["close"]).rolling(10).min().iloc[-1])
    crash = le10 - 0.05
    assert crash > entry_p - 2.0 * n0               # 场景自检:止损不该先触发
    ev = []
    w = _replay_weight(_df(closes + [crash]), **P, events=ev)
    assert w == 0.0
    assert "exit10" in _tags(ev) and "stop2n" not in _tags(ev)


def test_stop2n_hard_stop():
    """急跌穿 入场价 − 2N 但未破 10 日低 → stop2n(硬止损先于反向突破)。"""
    closes = _BASE + [10.2]                         # 突破入场,N≈0.2
    df = _df(closes)
    ev0 = []
    assert _replay_weight(df, **P, events=ev0) > 0
    e = next(i for i, t in ev0 if t == "entry20")
    entry_p = float(df["close"].iloc[e])
    n0 = float(_atr(df, P["atr_n"])[e - 1])
    le10 = float(pd.Series(df["close"]).rolling(10).min().iloc[-1])
    crash = entry_p - 2.0 * n0 - 0.02
    assert crash > le10                             # 场景自检:未破 10 日低
    ev = []
    w = _replay_weight(_df(closes + [crash]), **P, events=ev)
    assert w == 0.0
    assert "stop2n" in _tags(ev) and "exit10" not in _tags(ev)


# ---------------------------------------------------------------- 过滤器与兜底
def _win_then_rebreakout():
    """第一笔突破盈利(theo_win)→ 横盘衰减 → 再破 20 日高但低于 55 日高 13.7。"""
    up = list(9.7 + 0.4 * np.arange(1, 11))         # → 13.7
    down = [12.9, 12.1, 11.3, 10.9]                 # 跌破 10 日低,盈利平仓
    side = list(11.0 - 0.005 * np.arange(16))       # 20 日通道衰减
    re_up = list(11.2 + 0.25 * np.arange(1, 10))    # → 13.45,只破 20 日高
    return _df(_BASE + up + down + side + re_up)


def test_filter_skips_after_win():
    """上次突破盈利 → 本次 20 日突破被跳过(skip_filter),空仓等待。"""
    ev = []
    w = _replay_weight(_win_then_rebreakout(), **P, events=ev)
    tags = _tags(ev)
    assert "theo_win" in tags                       # 前置:上一笔确实盈利
    assert "skip_filter" in tags                    # 过滤器确实触发
    assert tags.index("theo_win") < tags.index("skip_filter")
    assert tags.count("entry20") == 1               # 第二次突破没有真实入场
    assert w == 0.0                                 # 被跳过期间空仓


def test_failsafe_55d_entry_after_skip():
    """被跳过后继续走强,突破 55 日高(13.7)→ 兜底入场,永不错过大行情。"""
    base = _win_then_rebreakout()
    ext = list(13.45 + 0.4 * np.arange(1, 8))       # → 16.25,穿越 55 日高
    df = _df(list(base["close"]) + ext)
    ev = []
    w = _replay_weight(df, **P, events=ev)
    tags = _tags(ev)
    assert "skip_filter" in tags
    assert "entry55_failsafe" in tags
    assert tags.index("skip_filter") < tags.index("entry55_failsafe")
    assert w > 0


def test_filter_allows_after_loss():
    """上次突破亏损(theo_loss)→ 下次 20 日突破放行(entry20)。"""
    seq = _BASE + [9.9] + [9.4] + list(9.4 - 0.005 * np.arange(20)) + [9.55, 9.75]
    ev = []
    w = _replay_weight(_df(seq), **P, events=ev)
    tags = _tags(ev)
    assert "theo_loss" in tags
    later = tags[tags.index("theo_loss"):]
    assert "entry20" in later and "skip_filter" not in later
    assert w > 0


def test_use_filter_false_is_plain_20_10():
    """use_filter=False:同一盈利后再突破序列直接第二次 entry20,无 skip。"""
    ev = []
    w = _replay_weight(_win_then_rebreakout(), **dict(P, use_filter=False), events=ev)
    tags = _tags(ev)
    assert "skip_filter" not in tags
    assert tags.count("entry20") == 2               # 两次突破都做
    assert w > 0


# ---------------------------------------------------------------- 无未来函数与配置
def test_no_lookahead():
    """today 截断:后接暴涨暴跌数据不改变截断日的输出。"""
    df = _df(_BASE + [10.1, 10.4])
    fn = get_strategy("turtle_s1")
    w0 = fn(_ctx(df), **P, lookback=750).get("510300", 0.0)
    future = _df(list(df["close"]) + [15.0, 5.0, 20.0])
    w1 = fn(_ctx(future, today=df.index[-1]), **P, lookback=750).get("510300", 0.0)
    assert w0 == pytest.approx(w1) and w0 > 0


def test_strategy_yaml():
    # 2026-08-09 策略精简后保留的最优池配置(原 7-ETF 版 turtle_s1_etf 已删除)
    cfg = load_strategy(ROOT / "config" / "strategies" / "turtle_s1_etf_hybrid26.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == (
        "turtle_s1_etf_hybrid26", "turtle_s1", "etf")
    assert len(cfg.universe) == 26
    assert cfg.params == {"n_entry": 20, "n_exit": 10, "n_failsafe": 55,
                          "atr_n": 20, "stop_n_mult": 2.0, "x_risk": 0.004375,
                          "cap": 0.10, "use_filter": True}
