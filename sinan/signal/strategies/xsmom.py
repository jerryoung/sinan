"""
横截面动量(截面排名 + 绝对动量过滤)——池内相对强弱轮动
====================================================================

与 donchian/turtle(时序型:每标的独立判断入出场)不同,本策略是
截面型:每日把 universe 内所有标的放在一起排名,只持有"最强"的一撮。

1. 动量度量(经典 12−1 月动量的日线版):
       mom = close[-1-skip_n] / close[-1-mom_n] − 1
   跳过最近 skip_n 根(默认 21 ≈ 1 个月)规避短期反转效应,
   回看 mom_n 根(默认 244 ≈ 12 个月)捕捉中期趋势持续性。
   bars 不足 mom_n + 2 根的标的直接跳过(指标不齐备,不参与排名)。

2. 绝对动量过滤:仅保留 mom > 0——熊市里"相对最强"可能照样在跌,
   叠加该时序过滤后,全池走弱时自然空仓(现金),不硬选矮子里的将军。

3. 截面排名:按 mom 降序取前 top_k 只(稳定排序,平手保持池内顺序)。

4. 定仓(模块 2 风险均衡,逐日重算):
       w = min(cap, x_risk / (atr_m × ATR(atr_n)末值 / close末值))
   ATR 末值不齐备(NaN)的标的直接跳过——与 donchian 入场 gate 同口径;
   Σw > 1 时等比缩到 1(现金 = 1 − Σw),与执行层风控互为双保险。

【与仓库"入场锁定"定仓约定的偏差(本策略唯一被允许的例外)】
donchian/turtle 在入场 bar 按初始止损幅度锁定权重、持有期间不变,
因为它们的风险账锚定在"入场时刻"。截面策略没有稳定的"入场 bar"
概念——成分每日随排名进出,同一标的可能反复进出前 top_k,窗口重放
中锚定某次"入场时刻"既无意义也不稳定,故权重逐日按当日 ATR/close
重算。由此带来的日频权重微动是无信息换手,由引擎
settings.execution.rebalance_band(|Δw| 小于带宽不调仓)统一抑制
——与 livermore 窗口漂移微动的处理方式一致;成分进出触发的整仓
换手才是本策略的真实信号,不受带宽影响。

其余工程约定与既有策略完全相同:纯函数,对 ctx.bars() 截至当日的
数据计算,无跨日缓存,回测/实盘同一路径;mom 与 ATR 只用截至当日
收盘的数据(mom 本身还额外滞后 skip_n 根),引擎负责 T+1;
可转债截至 today 已有强赎公告(EVT_REDEEM_ANNOUNCE)→ 强制 0。
"""
from __future__ import annotations

import numpy as np

from ..base import SignalContext, register
from ...universe.cb_terms import EVT_REDEEM_ANNOUNCE
from .livermore import _atr


@register("xsmom")
def xsmom(ctx: SignalContext, *, mom_n: int = 244, skip_n: int = 21,
          top_k: int = 5, atr_n: int = 20, atr_m: float = 3.0,
          x_risk: float = 0.025, cap: float = 1.0, lookback: int = 750,
          **_) -> dict[str, float]:
    """横截面动量:mom>0 过滤 → 按 mom 降序取 top_k → 风险均衡逐日定仓。"""
    scored: list[tuple[float, str, float]] = []   # (mom, sym, w)
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < mom_n + 2:
            continue
        close = df["close"].to_numpy()
        p_ref, p_base = close[-1 - skip_n], close[-1 - mom_n]
        if not (np.isfinite(p_ref) and np.isfinite(p_base)) or p_base <= 0:
            continue
        mom = p_ref / p_base - 1.0
        if mom <= 0:                              # 绝对动量过滤
            continue
        a, p = _atr(df, atr_n)[-1], close[-1]
        if not np.isfinite(a) or p <= 0:          # ATR 不齐备:不参与
            continue
        stop_frac = atr_m * a / p                 # 当日"若在此持仓"的止损幅度
        w = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
        scored.append((mom, sym, w))

    scored.sort(key=lambda t: t[0], reverse=True)  # 稳定排序,平手保持池内顺序
    targets = {sym: w for _, sym, w in scored[:top_k] if w > 0}

    total = sum(targets.values())
    if total > 1.0:
        targets = {s: w / total for s, w in targets.items()}
    return targets
