"""
时序动量 TSMOM(12-1)—— 经典 Moskowitz–Ooi–Pedersen 时序动量的长仓落地
====================================================================

规则(经典 12-1 口径,剔除最近一个月):

  动量   mom[i] = close[i-skip_n] / close[i-mom_n] − 1
         mom_n=244 ≈ 12 个月回看,skip_n=21 ≈ 剔除最近 1 个月——
         短期存在反转效应(上月涨的下月倾向回吐),经典 12-1 用
         "一个月前的价 ÷ 一年前的价"避开它。因此最近 skip_n 内的
         暴涨暴跌不改变当日信号:动量看的是 skip_n 之前的价格。
  持有   mom > 0 → 持有;mom ≤ 0 → 空仓。
         空仓时 mom > 0 → 入场并锁定权重;持有中 mom ≤ 0 → 清仓。
         i − mom_n < 0(窗口内历史不足一年)的 bar 直接跳过。
  定仓   模块 2 风险均衡,入场当日锁定
         w = min(cap, x_risk / stop_frac),
         stop_frac = atr_m × ATR(atr_n)[i-1] / close[i],
         持有期间不变。注意:ATR 只用于定仓,出场只看动量符号
         (TSMOM 无价格止损——趋势死了由动量翻负判定)。

与 donchian/turtle 相同的工程约定:对 lookback 窗口从空仓假设重放
状态机,返回"当日"目标权重——纯函数、回测/实盘同一路径;动量用
截至当日的收盘(close[i-skip_n]、close[i-mom_n] 均严格早于当日),
ATR 取前一日值 a[i-1],无未来函数;可转债强赎公告后权重 0;
组合 Σw > 1 等比缩。

events 非 None 时逐事件追加 (bar下标, 标签),标签 ∈ {'entry','exit'}
—— 供测试断言规则分支,生产传 None 零开销。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import SignalContext, register
from ...universe.cb_terms import EVT_REDEEM_ANNOUNCE
from .livermore import _atr


def _replay_weight(
    df: pd.DataFrame,
    *,
    mom_n: int,
    skip_n: int,
    atr_n: int,
    atr_m: float,
    x_risk: float,
    cap: float,
    events: list | None = None,
) -> float:
    """对单标的窗口重放 TSMOM 状态机,返回末根 K 线上的目标权重。"""
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    start = max(mom_n, skip_n, 1)          # i−mom_n<0 的 bar 跳过;i−1≥0
    if n < start + 1:
        return 0.0
    a = _atr(df, atr_n)                    # livermore._atr → np.ndarray

    def _emit(i, tag):
        if events is not None:
            events.append((i, tag))

    holding, w = False, 0.0
    for i in range(start, n):
        past, base = close[i - skip_n], close[i - mom_n]
        if not (np.isfinite(past) and np.isfinite(base)) or base <= 0:
            continue                        # 数据缺口:该 bar 不产生信号
        mom = past / base - 1.0
        if holding:
            if mom <= 0:                    # 动量翻负 → 清仓
                holding, w = False, 0.0
                _emit(i, "exit")
        else:
            ap = a[i - 1]
            if mom > 0 and np.isfinite(ap) and ap > 0:
                stop_frac = atr_m * ap / close[i]   # 入场时刻的初始止损幅度
                w = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
                holding = True              # 权重入场锁定,持有期间不变
                _emit(i, "entry")
    return w if holding else 0.0


@register("tsmom")
def tsmom(
    ctx: SignalContext,
    *,
    mom_n: int = 244,
    skip_n: int = 21,
    atr_n: int = 20,
    atr_m: float = 3.0,
    x_risk: float = 0.025,
    cap: float = 1.0,
    lookback: int = 750,
    **_,
) -> dict[str, float]:
    """时序动量 12-1:mom = close[t−skip]/close[t−mom_n] − 1,正持有负空仓。"""
    out: dict[str, float] = {}
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < mom_n + 1:             # 不足一年历史:不参与
            continue
        w = _replay_weight(df, mom_n=mom_n, skip_n=skip_n, atr_n=atr_n,
                           atr_m=atr_m, x_risk=x_risk, cap=cap)
        if w > 0:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return out
