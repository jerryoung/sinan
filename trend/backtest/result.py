"""回测结果容器:engine 产出、report/脚本消费的公共契约。"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# trades 列约定:date, symbol, side('buy'|'sell'), qty, price, cost, reason
# reason ∈ {'signal', 'force_delist', 'force_redeem'}
TRADE_COLS = ["date", "symbol", "side", "qty", "price", "cost", "reason"]


@dataclass
class BacktestResult:
    nav: pd.Series                      # 逐日净值,初始 1.0,index=交易日
    returns: pd.Series                  # 逐日收益率
    weights: pd.DataFrame               # 逐日实际持仓权重(index=date, columns=symbol)
    trades: pd.DataFrame                # 成交流水(TRADE_COLS)
    meta: dict = field(default_factory=dict)   # 策略名/参数/区间/成本假设等留痕
