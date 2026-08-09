"""
利维摩尔策略 ——《股票作手操盘术》思路的系统化落地
====================================================================

把书中四个可量化的思想融入 generate_targets(书为扫描版,规则依据
仓库先行研究 livermore_backtest.py 对第 9 章"市场要诀"十条规则的蒸馏):

1. 市场要诀六状态状态机(第 9 章,趋势域过滤器):
       上涨趋势 ⇄ 自然回调 ⇄ 次级回升   (多头域,持仓域)
       下跌趋势 ⇄ 自然反弹 ⇄ 次级回调   (空头域,空仓域)
   换栏阈值 θ = k_theta × ATR(规则 4/6;量纲实验结论:ATR 倍数
   在波动率相差 10 倍的资产上交易频率尺度不变,绝对价差已被处决);
   确认阈值 c = θ/2(规则 5/10,穿越关键点的确认)。

2. 关键点 + 时间要素:不预测、只等待——仅在空头域→多头域的
   关键点穿越"确认"时刻入场(自然反弹升破上一关键点 c,或深反弹
   2θ 兜底)。确认阈值就是时间要素的价格化:宁可晚入,不抢跑。

3. 试探—加码,永不摊低成本(资金管理核心):仓位分 n_tranches 批,
   突破确认先上试探仓 1/n;此后价格每上行 add_atr × 入场时ATR,
   加一批;只在"价格证明你对"时加码,下跌永不补仓。

4. 危险信号 + 快速认错:
   a) 自然回调跌破上一关键点 c(规则 10b)→ 全部离场,转空头域;
   b) 试探仓亏损即认错——收盘跌破 入场价 − stop_atr × 入场时ATR
      → 立即全平并转空头域,等待新的关键点确认(书中"亏损不该
      超过用于试探的少量筹码"的现代化:ATR 距离代替 10% 定率)。

定仓沿用三模块框架的模块 2:满仓权重 w_full = min(cap, x_risk /
stop_frac),入场锁定;实际权重 = w_full × 已建批数 / n_tranches。
stop_frac 的口径由 size_by 选择(对照实验参数;两者都不是"单笔风险
恰等于 x",诚实的风险账如下):
  "probe"  = stop_atr × 入场ATR / 入场价。认错止损线锚在入场价,
             加码批在更高价位买入、共用同一条线,加满后最坏损失
             ≈ (1 + (n−1)·add_atr/(2·stop_atr))·x ≈ 4/3·x(默认参数);
  "danger" = 2·k_theta × 入场ATR / 入场价 —— 按深回调兜底距离定仓的
             保守口径。因认错止损(entry − stop_atr·ATR)几乎总是先于
             2θ 兜底触发,实际单笔风险显著小于 x(≈0.3x,安全边际),
             等价于把仓位整体缩至 probe 口径的 stop_atr/(2·k_theta)。
             probe vs danger 的收益/回撤对比因此含仓位杠杆差,
             夏普/卡玛才是可比指标。
边界条件(比 donchian 更强):窗口重放的收敛依赖窗口内出现过一次
完整的空头域重同步(2θ 级回调)。若单段多头超过 lookback−atr_n 根
仍无此级别回调,入场锚会随窗口左缘漂移,目标权重产生无信号的日度
微动(实测 |Δw| 均值 ~0.006,多被 rebalance_band=0.02 吸收);
低波动标的或调小 k_theta 时,可加大 lookback 消除。

与 donchian 相同的工程约定:对 lookback 窗口从空仓假设重放状态机,
得到"当日"目标权重——纯函数、回测/实盘同一路径;全部阈值用前一日
ATR(a[i-1])与当日收盘判断,无未来函数;可转债有强赎公告 → 权重 0。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import SignalContext, register
from ...universe.cb_terms import EVT_REDEEM_ANNOUNCE


def _atr(df: pd.DataFrame, n: int) -> np.ndarray:
    """真实波幅均值:TR = max(H−L, |H−昨C|, |L−昨C|) 的 n 日均值。"""
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean().values


def _replay_weight(
    df: pd.DataFrame,
    *,
    k_theta: float,
    atr_n: int,
    n_tranches: int,
    add_atr: float,
    stop_atr: float,
    x_risk: float,
    cap: float,
    size_by: str = "probe",
    events: list | None = None,
) -> float:
    """
    对单标的窗口重放"市场要诀状态机 + 试探加码仓位层",返回最后一根的
    目标权重。窗口起点假设空头域空仓(与 livermore_backtest.py 一致)。

    events 非 None 时逐事件追加 (bar下标, 标签),标签 ∈
    {'entry','add','probe_stop','danger_10b','deep_reaction','recover_10a'}
    —— 供测试断言"具体哪条规则触发",生产调用传 None 零开销。
    """
    close = df["close"].values
    a = _atr(df, atr_n)
    n = len(close)
    if n < atr_n + 2:
        return 0.0

    # ---- 状态机层(与 livermore_backtest.livermore_signal(mode="atr") 同构) ----
    regime, sub = "bear", "trend"
    trend_high = trend_low = close[0]
    reaction_low = rally_high = np.nan
    key_reaction_low = key_rally_high = np.nan

    # ---- 仓位层:试探—加码 ----
    w_full = 0.0          # 入场锁定的满仓权重(模块 2 定仓)
    tranches = 0          # 已建批数(0 = 空仓)
    entry_price = np.nan  # 试探仓入场价(加码与认错止损的锚)
    base_atr = np.nan     # 入场时 ATR(a[i-1] 锁定)

    def _emit(i, tag):
        if events is not None:
            events.append((i, tag))

    def _flat():
        nonlocal w_full, tranches, entry_price, base_atr
        w_full, tranches, entry_price, base_atr = 0.0, 0, np.nan, np.nan

    def _to_bear(p):
        nonlocal regime, sub, trend_low, key_rally_high, rally_high
        regime, sub = "bear", "trend"
        trend_low = p
        rally_high = np.nan
        key_rally_high = np.nan
        _flat()

    start = atr_n + 1     # 等 ATR 就绪(阈值用 a[i-1])
    for i in range(start, n):
        p = close[i]
        ap = a[i - 1]
        if np.isnan(ap) or ap <= 0:
            continue
        th = k_theta * ap
        c = th / 2.0

        if regime == "bull":
            # ---- 危险信号 b:试探仓认错止损(仓位层,先于状态机判断) ----
            if tranches > 0 and p <= entry_price - stop_atr * base_atr:
                _emit(i, "probe_stop")
                _to_bear(p)
                continue

            if sub == "trend":
                trend_high = max(trend_high, p)
                if trend_high - p >= th:                  # 规则 6a:转自然回调
                    sub, reaction_low = "reaction", p
            else:
                reaction_low = min(reaction_low, p)
                if p >= trend_high + c:                   # 规则 10a:趋势恢复确认
                    sub = "trend"
                    key_reaction_low = reaction_low       # 规则 8:记录关键点
                    trend_high = p
                    _emit(i, "recover_10a")
                elif (not np.isnan(key_reaction_low)
                      and p <= key_reaction_low - c):     # 规则 10b:危险信号 a
                    _emit(i, "danger_10b")
                    _to_bear(p)
                    continue
                elif trend_high - p >= 2 * th:            # 深回调兜底
                    _emit(i, "deep_reaction")
                    _to_bear(p)
                    continue

            # ---- 加码:价格每上行 add_atr×入场ATR 加一批,永不向下摊 ----
            if 0 < tranches < n_tranches:
                while (tranches < n_tranches
                       and p >= entry_price + tranches * add_atr * base_atr):
                    tranches += 1
                    _emit(i, "add")
        else:
            if sub == "trend":
                trend_low = min(trend_low, p)
                if p - trend_low >= th:                   # 转自然反弹
                    sub, rally_high = "rally", p
            else:
                rally_high = max(rally_high, p)
                if p <= trend_low - c:                    # 下跌恢复确认
                    sub = "trend"
                    key_rally_high = rally_high           # 规则 8
                    trend_low = p
                else:
                    confirmed = (
                        (not np.isnan(key_rally_high) and p >= key_rally_high + c)
                        or p - trend_low >= 2 * th        # 深反弹兜底(首次入场)
                    )
                    if confirmed:                         # 规则 10d:关键点穿越确认
                        regime, sub = "bull", "trend"
                        trend_high = p
                        reaction_low = np.nan
                        key_reaction_low = np.nan
                        # ---- 试探仓:1/n 批,模块 2 定仓入场锁定 ----
                        entry_price, base_atr = p, ap
                        dist = (2 * k_theta if size_by == "danger" else stop_atr)
                        stop_frac = dist * ap / p
                        w_full = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
                        tranches = 1
                        _emit(i, "entry")

    return w_full * tranches / n_tranches if tranches > 0 else 0.0


@register("livermore")
def livermore(
    ctx: SignalContext,
    *,
    k_theta: float = 5.0,
    atr_n: int = 20,
    n_tranches: int = 3,
    add_atr: float = 1.0,
    stop_atr: float = 3.0,
    x_risk: float = 0.025,
    cap: float = 1.0,
    size_by: str = "probe",
    lookback: int = 750,
    **_,
) -> dict[str, float]:
    """利维摩尔:市场要诀状态机 + 关键点确认入场 + 试探加码 + 危险信号离场。"""
    out: dict[str, float] = {}
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue                          # 强赎公告后不接飞刀
        df = ctx.bars(sym, lookback)
        if len(df) < atr_n + 2:
            continue
        w = _replay_weight(df, k_theta=k_theta, atr_n=atr_n,
                           n_tranches=n_tranches, add_atr=add_atr,
                           stop_atr=stop_atr, x_risk=x_risk, cap=cap,
                           size_by=size_by)
        if w > 0:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0:                           # 现金 = 1 − Σw,超 1 等比缩
        out = {s: w / total for s, w in out.items()}
    return out
