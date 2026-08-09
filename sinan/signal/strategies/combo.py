"""
组合策略(strategy of strategies)——多条策略腿按资金权重合成一个目标仓位。

动机:回测发现海龟(突破族)与截面动量(相对强弱族)相关性仅 0.57,
50/50 组合三项指标全面优于单策略。与其让实盘跑两份 YAML、手工拆资金、
出两份 targets 文件,不如在信号层合成:每条腿是一个已注册策略,
腿内权重 × 腿资金占比后逐标的相加——**一份配置、一份 targets、
一次对账**,回测与实盘继续走同一条路径。

params.legs 形如:
    legs:
      - strategy: turtle_s1
        weight: 0.5              # 腿的资金占比(Σ 应 ≤ 1)
        params: {n_entry: 20, ..., x_risk: 0.00875, cap: 0.20}
      - strategy: xsmom
        weight: 0.5
        params: {mom_n: 244, ..., lookback: 750}

约定:腿 params 里可自带 lookback(缺省用组合层 lookback);
腿策略自身已处理强赎守卫与自身 Σ>1 缩放,组合层最后再做一次
全局 Σ>1 等比缩(两级缩放语义:先腿内、后组合,均只缩不放)。
与"两个独立账户各跑一腿"的差异:合成后共享同一 rebalance_band 与
手数取整,微小且方向保守(小腿差额更易被带宽过滤)。
"""
from __future__ import annotations

from ..base import SignalContext, get_strategy, register


@register("combo")
def combo(ctx: SignalContext, *, legs: list, lookback: int = 750, **_) -> dict[str, float]:
    """多策略腿资金加权合成:Σ_legs weight × leg_targets,再全局 Σ>1 缩。"""
    out: dict[str, float] = {}
    for leg in legs:
        fn = get_strategy(str(leg["strategy"]))
        p = dict(leg.get("params") or {})
        lb = int(p.pop("lookback", lookback))
        lw = float(leg.get("weight", 1.0 / len(legs)))
        for sym, w in (fn(ctx, **p, lookback=lb) or {}).items():
            out[sym] = out.get(sym, 0.0) + lw * float(w)

    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return {s: w for s, w in out.items() if w > 0}
