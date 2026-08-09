"""
超级趋势(SuperTrend)策略 —— ATR 通道棘轮跟踪的经典实现。

指标(标准定义,hl2 = (high+low)/2,ATR 为 atr_n 日 TR 均值):
    基础上轨 ub = hl2 + mult×ATR;基础下轨 lb = hl2 − mult×ATR
    终值上轨 fu[i] = min(ub[i], fu[i-1])  若 close[i-1] <= fu[i-1],否则 ub[i]
    终值下轨 fl[i] = max(lb[i], fl[i-1])  若 close[i-1] >= fl[i-1],否则 lb[i]
    趋势:up 期间 close[i] < fl[i] → 翻 down;down 期间 close[i] > fu[i] → 翻 up
下轨只升不降(棘轮),效果上与吊灯止损同族,但翻多需收盘上穿上轨,
带自适应的入场确认;仅做多:trend=up 持有,trend=down 空仓。

定仓沿用模块 2:翻多 bar 锁定 w = min(cap, x_risk/stop_frac),
stop_frac = mult×ATR[i]/close[i](即入场时到下轨的距离)。指标用含当日
的 ATR(SuperTrend 标准口径;数据仍截至当日收盘,无未来函数,执行由
引擎 T+1)。窗口重放、强赎守卫、Σ>1 缩放与 donchian 完全一致。

events 非 None 时追加 (bar下标, 'flip_up'|'flip_down'),供测试断言分支。
"""
from __future__ import annotations

import numpy as np

from ..base import SignalContext, register
from ...universe.cb_terms import EVT_REDEEM_ANNOUNCE
from .livermore import _atr


def _replay_weight(df, *, atr_n: int, mult: float, x_risk: float, cap: float,
                   events: list | None = None) -> float:
    close = df["close"].to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    a = _atr(df, atr_n)
    n = len(close)
    if n < atr_n + 2:
        return 0.0

    trend = -1                      # 窗口起点假设空头(与其余策略同:空仓起步)
    fu = fl = np.nan
    w = 0.0
    start = atr_n + 1
    for i in range(start, n):
        if not np.isfinite(a[i]):
            continue
        ub, lb = hl2[i] + mult * a[i], hl2[i] - mult * a[i]
        fu = ub if (not np.isfinite(fu)) or close[i - 1] > fu else min(ub, fu)
        fl = lb if (not np.isfinite(fl)) or close[i - 1] < fl else max(lb, fl)

        if trend == 1 and close[i] < fl:
            trend, w = -1, 0.0
            if events is not None:
                events.append((i, "flip_down"))
        elif trend == -1 and close[i] > fu:
            trend = 1
            stop_frac = mult * a[i] / close[i]
            w = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
            if events is not None:
                events.append((i, "flip_up"))
    return w if trend == 1 else 0.0


@register("supertrend")
def supertrend(ctx: SignalContext, *, atr_n: int = 10, mult: float = 3.0,
               x_risk: float = 0.025, cap: float = 1.0, lookback: int = 750,
               **_) -> dict[str, float]:
    """SuperTrend:上穿上轨翻多入场,跌破棘轮下轨翻空清仓。"""
    out: dict[str, float] = {}
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < atr_n + 2:
            continue
        w = _replay_weight(df, atr_n=atr_n, mult=mult, x_risk=x_risk, cap=cap)
        if w > 0:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return out
