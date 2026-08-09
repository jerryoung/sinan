"""
唐奇安突破 + ATR 吊灯止损 + 风险均衡定仓 —— 系统首个策略(方案 §7.1)。

语义与老脚本 atr_portfolio_backtest.py 的 signal()/position_weight() 逐行对齐:

  入场   close[i] ≥ he[i-1]
         he = n_entry 日收盘 rolling max(含当日),取 i-1 即"前一日的通道上沿"
  出场   exit_mode='atr'     : close[i] ≤ hh − m·ATR[i-1](hh = 入场以来最高收盘,
                               吊灯止损:止损距离随波动率伸缩)
         exit_mode='donchian': close[i] ≤ le[i-1](前一日的 n_exit 日收盘最低)
  定仓   入场当日锁定 w = min(cap, x_risk / stop_frac),
         stop_frac = m·ATR[i-1] / close[i](初始止损幅度),持有期间不变。
         这是模块 2 的风险均衡:波动大的标的自动拿小仓位,
         每笔交易对净值的最大伤害都 ≈ x_risk。

实现方式(方案 §7.1/§9 回测–实盘一致性):每天对 lookback 窗口从起点
以空仓假设重放上述状态机,输出"当日"的目标权重 —— 纯函数,不下单、
不读实时接口、不知道自己在回测还是实盘;回测引擎与实盘脚本构造各自的
SignalContext 走同一条路径。防未来函数双保险:指标一律取 T-1 值判断
T 日,而 ctx.bars() 又在接口层物理截断到 today 收盘。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...universe.cb_terms import EVT_REDEEM_ANNOUNCE
from ..base import SignalContext, register


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    """真实波幅均值:TR = max(H−L, |H−昨C|, |L−昨C|) 的 n 日 rolling mean。"""
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _replay_weight(df: pd.DataFrame, *, n_entry: int, n_exit: int, atr_n: int,
                   atr_m: float, x_risk: float, cap: float, exit_mode: str) -> float:
    """
    从窗口起点以空仓假设重放状态机,返回最后一根 K 线上的目标权重。

    只要 lookback 覆盖最长持仓周期,窗口起点的空仓假设就与全历史重放
    收敛到同一状态 —— 每天重算,不依赖任何跨日缓存,回测与实盘天然同路径。
    """
    close = df["close"].to_numpy()
    he = df["close"].rolling(n_entry).max().to_numpy()   # 入场通道(取 i-1 用)
    le = df["close"].rolling(n_exit).min().to_numpy()    # Donchian 出场线
    a = _atr(df, atr_n).to_numpy()

    holding, hh, w = 0, -np.inf, 0.0                     # hh = 入场以来最高收盘
    for i in range(1, len(df)):
        if holding == 0:
            if not np.isnan(he[i - 1]) and not np.isnan(a[i - 1]) and close[i] >= he[i - 1]:
                holding, hh = 1, close[i]
                stop_frac = atr_m * a[i - 1] / close[i]  # 入场时刻的初始止损幅度
                w = min(cap, x_risk / stop_frac) if stop_frac > 0 else cap
        else:
            hh = max(hh, close[i])
            if exit_mode == "atr":
                if close[i] <= hh - atr_m * a[i - 1]:    # 跌破吊灯线
                    holding, hh, w = 0, -np.inf, 0.0
            else:
                if not np.isnan(le[i - 1]) and close[i] <= le[i - 1]:
                    holding, hh, w = 0, -np.inf, 0.0
    return w


@register("donchian")
def donchian(ctx: SignalContext, *, n_entry: int = 55, n_exit: int = 20,
             atr_n: int = 20, atr_m: float = 3.0, x_risk: float = 0.025,
             cap: float = 1.0, exit_mode: str = "atr", lookback: int = 750,
             **_) -> dict[str, float]:
    """
    对 ctx.universe() 每个标的重放状态机,返回目标权重 {symbol: weight}。

    - 窗口不足 n_entry + atr_n 根:指标不齐备,该标的强制 0(不参与)。
    - 可转债:截至 today 已有强赎公告(EVT_REDEEM_ANNOUNCE)→ 权重强制 0,
      强赎公告后转债溢价会被强制转股/赎回击穿,不接飞刀。
    - 组合层面 Σw > 1 时按比例缩到 1(现金 = 1 − Σw),与执行层
      max_total_weight 风控互为双保险。只返回权重 > 0 的标的。
    """
    targets: dict[str, float] = {}
    for sym in ctx.universe():
        terms = ctx.cb_terms(sym)
        if terms is not None and terms.has_event(EVT_REDEEM_ANNOUNCE, until=ctx.today):
            continue
        df = ctx.bars(sym, lookback)
        if len(df) < n_entry + atr_n:
            continue
        w = _replay_weight(df, n_entry=n_entry, n_exit=n_exit, atr_n=atr_n,
                           atr_m=atr_m, x_risk=x_risk, cap=cap, exit_mode=exit_mode)
        if w > 0:
            targets[sym] = w

    total = sum(targets.values())
    if total > 1.0:
        targets = {s: w / total for s, w in targets.items()}
    return targets
