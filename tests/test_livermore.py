"""利维摩尔策略测试:试探—加码/认错止损/危险信号/无未来函数 + 与老脚本状态机差分。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.config import load_rules, load_strategy
from trend.signal.base import SignalContext, get_strategy
from trend.signal.strategies.livermore import _replay_weight
from trend.universe.cb_terms import EVT_REDEEM_ANNOUNCE, CBEvent, CBTerms
from trend.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]

P = dict(k_theta=5.0, atr_n=20, n_tranches=3, add_atr=1.0,
         stop_atr=3.0, x_risk=0.025, cap=1.0)


def _df(closes, spread=0.5):
    """收盘序列 → OHLC 帧(高低点给固定小幅带宽,ATR 可控)。"""
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range("2020-01-01", periods=len(c), freq="B")
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread,
                         "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)


def _ctx(df, today=None, sym="510300", cb=None):
    rules = {sym: resolve_rule(sym, "etf", load_rules())}
    return SignalContext(today=today or df.index[-1], data={sym: df},
                         positions={}, total_asset=1e6, rules=rules,
                         universe=[sym], cb_terms=cb)


def _breakout_series():
    """横盘 30 根(ATR≈spread 量级)→ 深反弹突破确认 → 持续上行。

    价格 10 横盘,spread=0.1 → ATR≈0.2,θ=1.0,c=0.5;
    随后逐步抬升穿越 2θ 深反弹兜底 → 多头域确认(试探仓)。
    """
    flat = [10.0] * 30
    rise = list(10.0 + 0.3 * np.arange(1, 25))       # 每日 +0.3,累计 +7.2
    return _df(flat + rise, spread=0.1)


# ---------------------------------------------------------------- 试探—加码
def test_probe_then_pyramid():
    df = _breakout_series()
    fn = get_strategy("livermore")

    # 找到首次出现权重的那天:应为 1/3 试探仓,权重 = w_full/3
    ws = []
    for t in df.index[25:]:
        w = fn(_ctx(df, today=t), **P, lookback=750)
        ws.append(w.get("510300", 0.0))
    ws = pd.Series(ws, index=df.index[25:])
    nonzero = ws[ws > 0]
    assert len(nonzero) > 0, "上行序列必须触发入场"
    first = nonzero.iloc[0]

    # 试探仓 = w_full × 1/3;末端持续上行后应加满 = 3 × 试探仓
    assert nonzero.iloc[-1] == pytest.approx(3 * first, rel=1e-9)
    # 批数单调不减(只加不减,直至离场)
    steps = (nonzero / first).round(6)
    assert steps.is_monotonic_increasing
    assert set(steps.unique()) <= {1.0, 2.0, 3.0}


def _append(df, closes, spread=0.1):
    """在帧尾追加收盘序列(不改动已有行,历史 ATR 不受影响)。"""
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range(df.index[-1] + pd.offsets.BDay(), periods=len(c), freq="B")
    ext = pd.DataFrame({"open": c, "high": c + spread, "low": c - spread,
                        "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)
    return pd.concat([df, ext])


def _entry_anchor(df):
    """重放并返回 (入场bar下标, 入场价, 入场ATR, 事件表)。要求确实入场。"""
    from trend.signal.strategies.livermore import _atr
    ev = []
    _replay_weight(df, **P, events=ev)
    tags = [t for _, t in ev]
    assert "entry" in tags, "前置条件:序列必须触发入场"
    e = next(i for i, t in ev if t == "entry")
    return e, float(df["close"].iloc[e]), float(_atr(df, P["atr_n"])[e - 1]), ev


def test_never_average_down():
    """入场后回落(未触发止损/危险信号)不加码;回升未过下一阈值也不加码。"""
    df = _breakout_series()
    e, entry, atr0, ev = _entry_anchor(df)
    a1 = next(i for i, t in ev if t == "add")          # 第一次加码 bar
    top = df.iloc[: a1 + 1]                            # 截断在两批仓状态
    ev_top = []
    w_top = _replay_weight(top, **P, events=ev_top)
    assert w_top > 0 and [t for _, t in ev_top].count("add") == 1

    # 回落 1×入场ATR(高于认错止损线),再回升但不过下一加码阈值
    dip = float(top["close"].iloc[-1]) - 1.0 * atr0
    back = float(top["close"].iloc[-1]) - 0.1
    assert dip > entry - P["stop_atr"] * atr0          # 场景自检:不该触发止损
    assert back < entry + 2 * P["add_atr"] * atr0      # 场景自检:不该触发加码
    ev_end = []
    w_end = _replay_weight(_append(top, [dip, dip, back]), **P, events=ev_end)
    tags = [t for _, t in ev_end]
    assert w_end == pytest.approx(w_top)               # 批数不变:永不向下摊
    assert tags.count("add") == 1                      # 回落/回升期间零加码
    assert "probe_stop" not in tags and "danger_10b" not in tags


# ---------------------------------------------------------------- 认错与危险信号
def test_probe_stop_cuts_loss():
    """跌破 入场价 − stop_atr×入场ATR → 认错止损全平(事件断言到具体分支)。"""
    df = _breakout_series()
    e, entry, atr0, _ = _entry_anchor(df)
    held = df.iloc[: e + 1]
    assert _replay_weight(held, **P) > 0               # 前置条件:确在持仓
    stop = entry - P["stop_atr"] * atr0
    ev = []
    w = _replay_weight(_append(held, [stop + 0.1, stop - 0.05]), **P, events=ev)
    tags = [t for _, t in ev]
    assert w == 0.0
    assert "probe_stop" in tags                        # 认错止损分支确实触发
    assert "danger_10b" not in tags and "deep_reaction" not in tags
    # 策略层同口径:generate_targets 返回空
    fn = get_strategy("livermore")
    assert fn(_ctx(_append(held, [stop + 0.1, stop - 0.05])), **P, lookback=750) == {}


def test_danger_signal_exits_all():
    """规则 10b:自然回调留下关键点 → 复升确认(10a)→ 跌破关键点 c → 全部离场。

    序列分五段:横盘 → 上行加满 → 回调 ~1.1θ(记 reaction_low)→ 复升过
    trend_high+c(10a,关键点落袋)→ 放量下跌(ATR 扩张使 10b 先于 2θ 兜底)。
    """
    df = _breakout_series()                            # 横盘30 + 上行24,H=17.2
    df = _append(df, [16.65, 16.10, 15.55, 15.00])     # 自然回调
    df = _append(df, [15.6, 16.2, 16.8, 17.4, 18.0, 18.6])   # 10a 恢复确认
    df = _append(df, [17.4, 16.2, 15.0, 13.8, 12.6, 11.4], spread=0.8)  # 放量下跌
    ev = []
    w = _replay_weight(df, **P, events=ev)
    tags = [t for _, t in ev]
    assert w == 0.0
    assert "entry" in tags and "recover_10a" in tags   # 关键点确实被记录过
    assert "danger_10b" in tags                        # 出场走的是规则 10b
    assert "deep_reaction" not in tags and "probe_stop" not in tags


# ---------------------------------------------------------------- 纯函数性质
def test_no_lookahead():
    df = _breakout_series()
    t = df.index[40]
    fn = get_strategy("livermore")
    w_before = fn(_ctx(df, today=t), **P, lookback=750)
    df2 = df.copy()
    df2.loc[df2.index > t, ["open", "high", "low", "close"]] = 1.0   # 未来暴跌
    w_after = fn(_ctx(df2, today=t), **P, lookback=750)
    assert w_before == w_after


def test_cb_redeem_forces_zero():
    df = _breakout_series()
    fn = get_strategy("livermore")
    rules = {"123078": resolve_rule("123078", "cb", load_rules())}

    def _w(today, cb):
        ctx = SignalContext(today=today, data={"123078": df}, positions={},
                            total_asset=1e6, rules=rules, universe=["123078"],
                            cb_terms=cb)
        return fn(ctx, **P, lookback=750)

    # 先定位无事件时的首个持仓日,再把强赎公告放在其后两天
    entry_day = next(t for t in df.index[25:] if _w(t, None) != {})
    ann = df.index[df.index.get_loc(entry_day) + 2]
    cb = {"123078": CBTerms(symbol="123078",
                            events=(CBEvent(date=ann, event=EVT_REDEEM_ANNOUNCE),))}
    assert _w(ann, cb) == {}                          # 公告日(含)权重强制 0
    prev = df.index[df.index.get_loc(ann) - 1]
    assert _w(prev, cb) != {}                         # 公告前一日不受影响


def test_weights_sum_capped():
    df = _breakout_series()
    syms = ["510300", "510500", "518880"]
    rules = {s: resolve_rule(s, "etf", load_rules()) for s in syms}
    ctx = SignalContext(today=df.index[-1], data={s: df for s in syms},
                        positions={}, total_asset=1e6, rules=rules,
                        universe=syms)
    fn = get_strategy("livermore")
    w = fn(ctx, **{**P, "x_risk": 0.4}, lookback=750)  # 触 cap 后 Σ 必超 1
    assert sum(w.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------- 与老脚本差分
def test_regime_matches_legacy_state_machine():
    """
    关掉试探止损(stop_atr=1e9)、单批建仓时,新策略在每个前缀窗口重放出的
    "是否持仓"必须与 livermore_backtest.livermore_signal(mode="atr") 的
    六状态状态机逐位一致 —— 证明思路蒸馏无损移植。
    """
    sys.path.insert(0, str(ROOT.parent))              # 仓库根(老脚本所在)
    from livermore_backtest import livermore_signal   # noqa: E402

    rng = np.random.default_rng(7)
    for trial in range(3):
        steps = rng.normal(0.001, 0.02, 260)
        closes = 10.0 * np.exp(np.cumsum(steps))
        df = _df(closes, spread=float(10.0 * 0.01))
        legacy = livermore_signal(df, mode="atr", k_atr=5.0)

        params = dict(P, n_tranches=1, stop_atr=1e9)
        for i in range(30, len(df), 7):               # 抽样前缀对比
            w = _replay_weight(df.iloc[: i + 1], **params)
            assert (w > 0) == (legacy.iloc[i] > 0), \
                f"trial={trial} i={i}: 新={w > 0} 老={legacy.iloc[i] > 0}"


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "livermore_etf.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == ("livermore_etf", "livermore", "etf")
    assert cfg.universe == ["510300", "510500", "159915", "512880",
                            "518880", "513100", "511010"]
    assert cfg.params["n_tranches"] == 3 and cfg.params["k_theta"] == 5.0
