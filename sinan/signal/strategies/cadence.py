"""
定期执行包装器(cadence)——把任意策略从"每日调仓"降频为"每周/每月调仓"。

动机:检验换仓时长对趋势系统的影响。日频是基准;降频省换手,但代价是
**止损与出场信号只在调仓日生效**——持有期内的破位要等到下一个调仓日
才能执行,回撤尾部理应变差。到底差多少,回测说话。

params:
    inner: {strategy: <已注册策略名>, params: {...}}   # 被包装的内层策略
    freq:  "D" 每日(透传,等价于直接跑内层)
           "W" 每周首个交易日执行;"M" 每月首个交易日执行
非调仓日返回当前持仓权重(目标=现状 → 引擎零委托,不产生任何交易,
仓位随价格自然漂移);调仓日执行内层策略并输出其目标。空仓起步时
同样等到首个调仓日才入场(降频语义要诚实,首日不特殊)。
周期首日判断与 rebalance 基准策略同法:任一标的截至今日的最后一根
bar 若属上一周期,则今日为周期首个交易日。
"""
from __future__ import annotations

import pandas as pd

from ..base import SignalContext, get_strategy, register


def _period_key(ts: pd.Timestamp, freq: str):
    if freq == "W":
        y, w, _ = ts.isocalendar()
        return int(y), int(w)
    if freq == "M":
        return ts.year, ts.month
    raise ValueError(f"未知 freq: {freq}")


@register("cadence")
def cadence(ctx: SignalContext, *, inner: dict, freq: str = "W",
            lookback: int = 750, **_) -> dict[str, float]:
    fn = get_strategy(str(inner["strategy"]))
    p = dict(inner.get("params") or {})
    lb = int(p.pop("lookback", lookback))
    if freq == "D":
        return fn(ctx, **p, lookback=lb)

    prev = None
    for s in ctx.universe():
        df = ctx.bars(s, 5)
        idx = df.index[df.index < ctx.today]
        if len(idx) and (prev is None or idx[-1] > prev):
            prev = idx[-1]
    is_schedule = prev is None or _period_key(ctx.today, freq) != _period_key(prev, freq)
    if is_schedule:
        return fn(ctx, **p, lookback=lb)
    return dict(ctx.positions)          # 周期中:目标=现状,零委托
