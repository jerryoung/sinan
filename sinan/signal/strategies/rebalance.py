"""
等权买入持有 + 周期再平衡 —— 趋势策略的"懒人基准"。

freq:
  "M" / "Q" / "Y"  每月 / 每季 / 每年的**首个交易日**把权重拉回目标;
  "N"              只在第一天建仓,此后永不再平衡(纯漂移持有)。

非再平衡日返回当前持仓权重(目标=现状 → 引擎零委托),所以周期之间
不产生任何交易;再平衡日的调仓同样经过引擎的成本/手数/涨跌停模型。
weights 缺省为 universe 等权(Σ=1,满仓);晚上市标的的份额在其上市后
的下一个再平衡日自然买入。周期首日判断用行情日期而非日历日:任一标的
截至今日的最后一根 bar 若属于上一周期,则今日为周期首个交易日。
"""
from __future__ import annotations

import pandas as pd

from ..base import SignalContext, register


def _period_key(ts: pd.Timestamp, freq: str):
    if freq == "M":
        return ts.year, ts.month
    if freq == "Q":
        return ts.year, (ts.month - 1) // 3
    if freq == "Y":
        return ts.year
    raise ValueError(f"未知 freq: {freq}")


@register("rebalance")
def rebalance(
    ctx: SignalContext,
    *,
    freq: str = "Q",
    weights: dict | None = None,
    lookback: int = 5,
    **_,
) -> dict[str, float]:
    syms = list(ctx.universe())
    tgt = ({str(s): float(w) for s, w in weights.items()} if weights
           else {s: 1.0 / len(syms) for s in syms})

    if not ctx.positions:                 # 尚未建仓:立即按目标入场
        return tgt
    if freq == "N":                       # 永不再平衡:维持现状
        return dict(ctx.positions)

    # 找今日之前的最后一个交易日(任一标的的行情日期并集)
    prev = None
    for s in syms:
        df = ctx.bars(s, lookback)
        idx = df.index[df.index < ctx.today]
        if len(idx) and (prev is None or idx[-1] > prev):
            prev = idx[-1]
    if prev is None:                      # 窗口内无历史:视为周期首日
        return tgt
    if _period_key(ctx.today, freq) != _period_key(prev, freq):
        return tgt                        # 周期首个交易日 → 拉回目标权重
    return dict(ctx.positions)            # 周期中:目标=现状,零委托
