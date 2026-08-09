"""
执行价模型(方案 §7.2)—— 撮合假设的唯一实现,engine 逐日循环第 1 步的依据。

三条撮合规则:

1. 执行价:price_mode('close'/'open',settings.execution.price_mode)的
   基准价 ± 滑点 —— 买 ×(1+slippage),卖 ×(1−slippage)。
   现金记账一律用**不复权**执行价(raw),与真实券商成交回报同口径;
   复权股数换算见 engine 的会计口径说明。

2. 涨跌停判定用**不复权价**:bars 自带 up_limit/down_limit 列;缺失时用
       round(pre_close × (1 ± limit_pct), n)
   推算,n = 股票/转债 2 位小数、ETF 3 位(交易所报价最小变动);
   pre_close 缺失时用前一日不复权收盘代替,首日两者皆无 → 不设限
   (NaN 视为无涨跌停约束)。
   当日不复权执行价 ≥ 涨停价 → 买单不成交;≤ 跌停价 → 卖单不成交
   (强制平仓除外)。注:滑点并入执行价后,临近涨停的买单会略保守地
   被判不成交 —— 这是刻意的偏保守假设。

3. 停牌:当日无 K 线 → 不成交。引擎每日重发目标权重,顺延自然成立,
   本模块无须任何专门状态。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..universe.instruments import TradingRule

#: 涨跌停推算的报价小数位:股票/转债 0.01 元,ETF 0.001 元
PRICE_DECIMALS = {"stock": 2, "cb": 2, "etf": 3}

_EPS = 1e-9


def price_decimals(sec_type: str) -> int:
    """该品种的报价小数位(未知品种按股票 2 位处理)。"""
    return PRICE_DECIMALS.get(sec_type, 2)


def prepare_market(df: pd.DataFrame, rule: TradingRule) -> pd.DataFrame:
    """
    单标的行情帧 → 执行帧(index=date,升序)。

    输入:DataStore.read_bars(adjust=True) 的单标的切片 ——
    open/high/low/close 为后复权价,close_raw 为不复权收盘,
    另含 adj_factor / pre_close / up_limit / down_limit(可能为 NaN)。

    输出补齐两类执行要素:
    - open_raw = 后复权 open ÷ adj_factor(开盘执行模式的不复权基准价);
    - up_limit / down_limit 缺失处用 round(pre_close × (1±limit_pct), n) 推算,
      pre_close 缺失处用前一日不复权收盘;首日无 pre_close → 保持 NaN(不设限)。
    """
    out = df.copy()
    if "date" in out.columns:
        out = out.set_index("date")
    out = out.sort_index()
    out = out.drop(columns=[c for c in ("symbol", "sec_type") if c in out.columns])

    # 复权因子:NaN/0 视为 1(不复权品种,如转债)
    f = pd.to_numeric(out.get("adj_factor"), errors="coerce").fillna(1.0).replace(0.0, 1.0)
    out["adj_factor"] = f
    out["open_raw"] = out["open"] / f

    pre = out["pre_close"] if "pre_close" in out.columns else pd.Series(np.nan, index=out.index)
    pre = pd.to_numeric(pre, errors="coerce").fillna(out["close_raw"].shift(1))

    nd = price_decimals(rule.sec_type)
    up_infer = (pre * (1 + rule.limit_pct)).round(nd)
    dn_infer = (pre * (1 - rule.limit_pct)).round(nd)
    for col, infer in (("up_limit", up_infer), ("down_limit", dn_infer)):
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(infer)
    return out


def exec_price(row, rule: TradingRule, side: str, price_mode: str) -> float:
    """不复权执行价:price_mode 基准价 × (1+slippage)(买)/(1−slippage)(卖)。"""
    base = float(row["open_raw"] if price_mode == "open" else row["close_raw"])
    if side == "buy":
        return base * (1 + rule.slippage)
    return base * (1 - rule.slippage)


def fillable(side: str, raw_exec: float, up_limit, down_limit, force: bool = False) -> bool:
    """
    涨跌停可成交判定(不复权口径)。

    买单:执行价 ≥ 涨停价 → 不成交(一字涨停买不进);
    卖单:执行价 ≤ 跌停价 → 不成交(一字跌停卖不出);
    force=True(强制平仓:退市/强赎末日)无视跌停 —— 终结性事件必须出清。
    """
    if force:
        return True
    if side == "buy" and pd.notna(up_limit) and raw_exec >= float(up_limit) - _EPS:
        return False
    if side == "sell" and pd.notna(down_limit) and raw_exec <= float(down_limit) + _EPS:
        return False
    return True
