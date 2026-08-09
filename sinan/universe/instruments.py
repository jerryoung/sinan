"""
品种规则层(方案 §6):一个 TradingRule 数据类把三类品种的差异参数化。

信号层、回测层、执行层都只通过 TradingRule 读取交易规则,
规则数值本身来自 config/rules.yaml,任何地方不硬编码。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingRule:
    symbol: str
    sec_type: str           # 'stock' | 'etf' | 'cb'
    t_plus: int             # 1 = 当日买入次日才可卖;0 = 回转交易
    lot_size: int           # 最小交易单位(股/份/张)
    commission: float       # 单边佣金率
    stamp_tax_sell: float   # 卖出印花税率(仅股票 > 0)
    slippage: float         # 单边滑点率(计入执行价)
    limit_pct: float        # 涨跌幅限制


def _stock_limit(symbol: str, name: str, cfg: dict) -> float:
    """板块推断:688/689 科创、300/301 创业 ±20%;主板 ST ±5%(双创 ST 仍 ±20%);8/4/92 开头北交所 ±30%。"""
    star = symbol.startswith(("688", "689", "300", "301"))
    if star:
        return cfg["limit_pct_star"]
    if symbol.startswith(("83", "87", "88", "43", "92")):
        return cfg["limit_pct_bj"]
    if "ST" in name.upper():
        return cfg["limit_pct_st"]
    return cfg["limit_pct_default"]


def _etf_limit(symbol: str, cfg: dict) -> float:
    if symbol in set(cfg.get("limit20_symbols", [])):
        return cfg["limit_pct_star"]
    if symbol.startswith(tuple(cfg.get("limit20_prefixes", []))):
        return cfg["limit_pct_star"]
    return cfg["limit_pct_default"]


def _etf_t_plus(symbol: str, cfg: dict) -> int:
    """跨境/债券/货币/黄金类 ETF T+0,按前缀近似识别,可用 t0_symbols 精确覆盖。"""
    if symbol in set(cfg.get("t0_symbols", [])):
        return 0
    if symbol.startswith(tuple(cfg.get("t0_prefixes", []))):
        return 0
    return cfg["t_plus"]


def resolve_rule(symbol: str, sec_type: str, rules_cfg: dict, name: str = "") -> TradingRule:
    """由 6 位代码 + 品种类型解析出该标的的全部交易规则参数。"""
    if sec_type not in rules_cfg:
        raise ValueError(f"未知品种类型: {sec_type}")
    cfg = rules_cfg[sec_type]

    if sec_type == "stock":
        limit, t_plus = _stock_limit(symbol, name, cfg), cfg["t_plus"]
    elif sec_type == "etf":
        limit, t_plus = _etf_limit(symbol, cfg), _etf_t_plus(symbol, cfg)
    else:  # cb
        limit, t_plus = cfg["limit_pct_default"], cfg["t_plus"]

    return TradingRule(
        symbol=symbol,
        sec_type=sec_type,
        t_plus=t_plus,
        lot_size=cfg["lot_size"],
        commission=cfg["commission"],
        stamp_tax_sell=cfg["stamp_tax_sell"],
        slippage=cfg["slippage"],
        limit_pct=limit,
    )
