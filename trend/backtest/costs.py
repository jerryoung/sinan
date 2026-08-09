"""
费用模型(方案 §7.2 第 4 步):佣金 + 印花税 + 滑点。

口径拆分(与 engine 的现金记账严格对应,避免重复计费):

- 滑点:已并入执行价(execution_model.exec_price 的 ±slippage),
  体现为"成交价劣于基准价",不在本模块重复扣减;
- 佣金:双边,gross × rule.commission;
- 印花税:仅卖出,gross × rule.stamp_tax_sell(股票 > 0,ETF/转债为 0)。

本模块产出**现金口径费用**,由引擎逐笔记入 trades.cost;
单笔现金变动 = 买:−(gross + cost),卖:+(gross − cost),
其中 gross = 不复权执行价 × 成交股数 —— 据此可与现金账逐分对账。
所有费率来自 TradingRule(config/rules.yaml),任何地方不硬编码。
"""
from __future__ import annotations

from ..universe.instruments import TradingRule


def trade_cost(side: str, gross: float, rule: TradingRule) -> float:
    """单笔现金费用:佣金(双边)+ 印花税(仅卖出)。gross = 不复权执行价 × 股数。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"未知交易方向: {side}")
    fee = gross * rule.commission
    if side == "sell":
        fee += gross * rule.stamp_tax_sell
    return fee


def cash_delta(side: str, gross: float, rule: TradingRule) -> float:
    """单笔成交对现金的净影响:买 = −(gross+费),卖 = +(gross−费)。"""
    fee = trade_cost(side, gross, rule)
    return -(gross + fee) if side == "buy" else gross - fee
