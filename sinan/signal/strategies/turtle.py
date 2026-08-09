"""
海龟系统 1(快系统)——《海龟交易法则》原始双系统的 S1 落地
====================================================================

原始海龟规则是快慢双系统并行:
  S1(快):20 日突破入场,反向 10 日突破出场,带"上次盈利则跳过"过滤器;
  S2(慢):55 日突破入场,反向 20 日突破出场,无过滤器。
仓库的 donchian 策略(55 日入场)本质上是 S2 变体(出场换吊灯止损),
本模块补上 S1,用于快慢系统对照与组合分散实验。

S1 规则(长仓版,按仓库约定收盘价判断、通道取前一日值、T+1 执行):

1. 入场:收盘 ≥ 前一日的 20 日收盘最高 → 突破信号。
2. 过滤器(S1 精髓):若"上一次 20 日突破"的理论交易(无论当时是否
   实际执行)最终盈利,则跳过本次信号——突破盈利有均值回归性。
   理论交易的账永远在记:每次 20 日突破都开一笔理论仓,按 S1 出场
   规则平仓,盈亏决定下一次信号是否放行。窗口起点无历史 → 首个信号
   放行(回测/实盘同一 lookback,口径一致)。
3. 兜底入场:收盘 ≥ 前一日的 55 日收盘最高 → 无视过滤器直接入场
   ——被过滤器跳过后若走成大趋势,55 日突破点强制上车,保证永不
   错过大行情(这是海龟敢用过滤器的前提)。
4. 出场(先到先执行):
   a) 反向突破:收盘 ≤ 前一日的 10 日收盘最低;
   b) 2N 硬止损:收盘 ≤ 入场价 − 2 × N(N = 入场时 ATR(20),锁定)。
5. 定仓:模块 2 风险均衡,w = min(cap, x_risk / (2N/入场价)),
   入场锁定。原始海龟的 ½N 加仓(最多 4 单位)此处不做——保持与
   donchian(单仓位)的对照口径干净;加码语义已由 livermore 承载。

工程约定与 donchian/livermore 相同:对 lookback 窗口从空仓假设重放,
返回当日目标权重;纯函数、回测/实盘同一路径;无未来函数;可转债
强赎公告后权重 0;Σ>1 等比缩。

events 非 None 时逐事件追加 (bar下标, 标签),标签 ∈
{'entry20','skip_filter','entry55_failsafe','exit10','stop2n',
 'theo_win','theo_loss'} —— 供测试断言具体规则分支,生产传 None 零开销。
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
    n_entry: int,
    n_exit: int,
    n_failsafe: int,
    atr_n: int,
    stop_n_mult: float,
    x_risk: float,
    cap: float,
    use_filter: bool = True,
    events: list | None = None,
) -> float:
    """对单标的窗口重放 S1 状态机(真实仓 + 理论仓双轨),返回末根目标权重。"""
    close = df["close"].values
    n = len(close)
    warm = max(n_entry, n_failsafe, atr_n) + 1
    if n < warm + 1:
        return 0.0

    a = _atr(df, atr_n)
    he_e = pd.Series(close).rolling(n_entry).max().values      # 20日入场通道
    he_f = pd.Series(close).rolling(n_failsafe).max().values   # 55日兜底通道
    le_x = pd.Series(close).rolling(n_exit).min().values       # 10日出场通道

    def _emit(i, tag):
        if events is not None:
            events.append((i, tag))

    # 真实仓
    holding, entry_price, entry_n, w_full = False, np.nan, np.nan, 0.0
    # 理论仓(过滤器的账本):每次 20 日突破必开,按同一出场规则结账
    theo_open, theo_entry, theo_n = False, np.nan, np.nan
    last_theo_win: bool | None = None      # None = 尚无历史,放行

    for i in range(warm, n):
        p = close[i]
        ap = a[i - 1]
        if np.isnan(ap) or ap <= 0 or np.isnan(he_e[i - 1]):
            continue

        # ---- 理论仓结账(先于开新仓判断,当日可先平后开) ----
        if theo_open and (p <= le_x[i - 1] or p <= theo_entry - stop_n_mult * theo_n):
            last_theo_win = bool(p > theo_entry)   # 必须转原生 bool:过滤器用 is 比较
            _emit(i, "theo_win" if last_theo_win else "theo_loss")
            theo_open = False

        # ---- 真实仓出场 ----
        if holding and (p <= le_x[i - 1] or p <= entry_price - stop_n_mult * entry_n):
            _emit(i, "exit10" if p <= le_x[i - 1] else "stop2n")
            holding, entry_price, entry_n, w_full = False, np.nan, np.nan, 0.0

        # ---- 入场判断 ----
        brk20 = p >= he_e[i - 1]
        brk55 = not np.isnan(he_f[i - 1]) and p >= he_f[i - 1]
        if brk20 and not theo_open:                     # 理论仓永远记账
            theo_open, theo_entry, theo_n = True, p, ap
        if not holding:
            allowed = (not use_filter) or (last_theo_win is not True)
            if brk20 and allowed:
                tag = "entry20"
            elif brk55:                                 # 兜底:无视过滤器
                tag = "entry55_failsafe"
            else:
                if brk20:
                    _emit(i, "skip_filter")
                continue
            holding, entry_price, entry_n = True, p, ap
            stop_frac = stop_n_mult * ap / p
            w_full = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
            _emit(i, tag)

    return w_full if holding else 0.0


@register("turtle_s1")
def turtle_s1(
    ctx: SignalContext,
    *,
    n_entry: int = 20,
    n_exit: int = 10,
    n_failsafe: int = 55,
    atr_n: int = 20,
    stop_n_mult: float = 2.0,
    x_risk: float = 0.025,
    cap: float = 1.0,
    use_filter: bool = True,
    lookback: int = 750,
    **_,
) -> dict[str, float]:
    """海龟 S1:20 日突破 + 盈利过滤器 + 55 日兜底 + 10日/2N 出场。"""
    out: dict[str, float] = {}
    for sym in ctx.universe():
        cb = ctx.cb_terms(sym)
        if cb is not None and cb.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < max(n_entry, n_failsafe, atr_n) + 2:
            continue
        w = _replay_weight(df, n_entry=n_entry, n_exit=n_exit,
                           n_failsafe=n_failsafe, atr_n=atr_n,
                           stop_n_mult=stop_n_mult, x_risk=x_risk,
                           cap=cap, use_filter=use_filter)
        if w > 0:
            out[sym] = w

    total = sum(out.values())
    if total > 1.0:
        out = {s: w / total for s, w in out.items()}
    return out
