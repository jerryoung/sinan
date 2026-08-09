"""
定投策略(DCA)—— 固定日程的虚拟账户重放,输出当日目标权重。

把"每期固定金额买入、只买不卖"翻译进系统的权重契约:策略内部维护一个
虚拟账户(初始现金 = capital),自 start 起每个周期首个交易日按 amount
等分买入篮子(可选下跌加码),份额只增不减;当日目标权重 =
各标的份额市值 ÷ (剩余现金 + 总市值)。纯函数重放:回测引擎与影子模式
喂同一段 bars 得到同一份权重——定投计划因此可以像趋势策略一样被
影子模式逐日跟踪、由引擎回测(费用/整手/T+1 由引擎与实盘各自处理,
虚拟账户本身不计费,避免双算)。

参数:
    start     定投起始日(YYYY-MM-DD,写死在配置里保证可复现)。
              该值锚定影子/实盘的真实计划;回测中引擎会将其覆盖为回测
              窗口起点("这个计划在这段历史上表现如何"),两者窗口对齐时
              结果一致
    freq      W/M/Q,周期首个交易日投入
    amount    每期投入总额(元),篮子内等分
    capital   虚拟账户本金(元);现金投完后计划自然停止
    dip_rule  下跌加码:none | dip2x(跌破年线2×)| dip2x_half(跌2×涨0.5×)
              | tiered(低于年线10%以上3×/低于年线2×/其余1×)
    ma_n      "历史平均"的均线窗口(默认 250 ≈ 年线)

注意:重放窗口受 lookback 限制——计划时长超过 lookback 会截断早期
买入,长期跟踪请把 lookback 调大到覆盖整个计划(750 ≈ 3 年)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import SignalContext, register


def _period_key(ts: pd.Timestamp, freq: str):
    if freq == "W":
        y, w, _ = ts.isocalendar()
        return int(y), int(w)
    if freq == "M":
        return ts.year, ts.month
    if freq == "Q":
        return ts.year, (ts.month - 1) // 3
    raise ValueError(f"未知 freq: {freq}")


def _multiplier(dip_rule: str, ratio: float) -> float:
    if dip_rule == "dip2x":
        return 2.0 if ratio < 1.0 else 1.0
    if dip_rule == "dip2x_half":
        return 2.0 if ratio < 1.0 else 0.5
    if dip_rule == "tiered":
        return 3.0 if ratio < 0.9 else (2.0 if ratio < 1.0 else 1.0)
    return 1.0


@register("dca")
def dca(
    ctx: SignalContext,
    *,
    start: str,
    amount: float = 4000.0,
    freq: str = "M",
    capital: float = 100000.0,
    dip_rule: str = "none",
    ma_n: int = 250,
    lookback: int = 750,
    **_,
) -> dict[str, float]:
    syms = list(ctx.universe())
    data = {s: ctx.bars(s, lookback) for s in syms}
    data = {s: df for s, df in data.items() if len(df)}
    if not data:
        return {}

    # 联合交易日历(截至 today)上的周期首日,从 start 起
    cal = sorted(set().union(*[set(df.index) for df in data.values()]))
    plan_days = [d for d in cal if d >= pd.Timestamp(start)]
    buys, seen = [], set()
    for d in plan_days:
        k = _period_key(d, freq)
        if k not in seen:
            seen.add(k)
            buys.append(d)

    cash = float(capital)
    units = {s: 0.0 for s in data}
    per = float(amount) / len(syms)
    for d in buys:
        if cash <= 1e-9:
            break
        for s, df in data.items():
            idx = df.index[df.index <= d]
            if not len(idx):
                continue                        # 该标的尚未上市:跳过其份额
            px = float(df["close"].loc[idx[-1]])
            if not np.isfinite(px) or px <= 0:
                continue
            mult = 1.0
            if dip_rule != "none":
                closes = df["close"].loc[:idx[-1]]
                if len(closes) >= ma_n:
                    mult = _multiplier(dip_rule, px / float(closes.tail(ma_n).mean()))
            spend = min(per * mult, cash)
            if spend <= 0:
                continue
            units[s] += spend / px
            cash -= spend

    total = cash
    vals: dict[str, float] = {}
    for s, df in data.items():
        px = float(df["close"].iloc[-1])
        v = units[s] * px if np.isfinite(px) else 0.0
        vals[s] = v
        total += v
    if total <= 0:
        return {}
    return {s: v / total for s, v in vals.items() if v / total > 1e-9}
