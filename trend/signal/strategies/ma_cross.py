"""
双均线(EMA 金叉/死叉)策略 —— 最朴素的趋势跟踪基准
====================================================================

规则(长仓版,收盘价判断、T+1 执行由引擎负责):

1. 均线:EMA(span=fast) 与 EMA(span=slow),pandas ewm(adjust=False)
   逐日递推 e[i] = α·c[i] + (1−α)·e[i−1],α = 2/(span+1)。
   EMA 在 i 只用截至 i 的收盘,无未来函数;引擎再做 T+1 执行。
2. 持有条件:ema_fast > ema_slow。
   空仓时当日 ema_f > ema_s → 金叉入场(窗口起点即满足则首个
   有效 bar 入场);持有时 ema_f ≤ ema_s → 死叉清仓。
   无止损、无通道——刻意保持极简,作为 donchian/turtle 的
   "指标平滑 vs 价格突破"对照基准。
3. 定仓:模块 2 风险均衡,入场 bar 锁定
       w = min(cap, x_risk / stop_frac),
       stop_frac = atr_m × ATR(atr_n)[i−1] / close[i],
   持有期间不变(与 donchian 完全同口径,ATR 取前一日值)。

工程约定与 donchian/turtle 相同:对 lookback 窗口从空仓假设重放
状态机,返回当日目标权重;纯函数、回测/实盘同一路径;可转债强赎
公告(EVT_REDEEM_ANNOUNCE)后权重 0;组合 Σw > 1 等比缩到 1。

预热:i 从 max(slow, atr_n) + 1 起——慢线在窗口前段仍带初值偏差,
统一跳过 slow 根让两条 EMA 都收敛后再开始判断,且保证 ATR[i−1] 有值。

events 非 None 时逐事件追加 (bar下标, 标签),标签 ∈ {'entry','exit'}
—— 供测试断言金叉/死叉的具体 bar,生产传 None 零开销。
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
    fast: int,
    slow: int,
    atr_n: int,
    atr_m: float,
    x_risk: float,
    cap: float,
    events: list | None = None,
) -> float:
    """对单标的窗口重放金叉/死叉状态机,返回最后一根 K 线的目标权重。"""
    close = df["close"].values
    n = len(close)
    warm = max(slow, atr_n) + 1
    if n < warm + 1:
        return 0.0

    ema_f = df["close"].ewm(span=fast, adjust=False).mean().values
    ema_s = df["close"].ewm(span=slow, adjust=False).mean().values
    a = _atr(df, atr_n)

    def _emit(i, tag):
        if events is not None:
            events.append((i, tag))

    holding, w = False, 0.0
    for i in range(warm, n):
        if holding:
            if ema_f[i] <= ema_s[i]:               # 死叉 → 清仓
                _emit(i, "exit")
                holding, w = False, 0.0
        else:
            ap = a[i - 1]
            if np.isnan(ap) or ap <= 0:            # ATR 未齐备无法定仓,顺延
                continue
            if ema_f[i] > ema_s[i]:                # 金叉 → 入场并锁定权重
                stop_frac = atr_m * ap / close[i]
                w = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
                holding = True
                _emit(i, "entry")
    return w if holding else 0.0


@register("ma_cross")
def ma_cross(
    ctx: SignalContext,
    *,
    fast: int = 20,
    slow: int = 60,
    atr_n: int = 20,
    atr_m: float = 3.0,
    x_risk: float = 0.025,
    cap: float = 1.0,
    lookback: int = 750,
    **_,
) -> dict[str, float]:
    """双均线:EMA(fast) 上穿 EMA(slow) 持有,下穿清仓,风险均衡定仓。"""
    out: dict[str, float] = {}
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < max(slow, atr_n) + 2:
            continue
        w = _replay_weight(df, fast=fast, slow=slow, atr_n=atr_n,
                           atr_m=atr_m, x_risk=x_risk, cap=cap)
        if w > 0:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return out
