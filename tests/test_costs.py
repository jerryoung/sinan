"""
Task 5 费用与执行价模型测试:
- costs.trade_cost / cash_delta 逐项口径
- execution_model 的执行价、涨跌停推算(股票 2 位小数 / ETF 3 位)与可成交判定
- 引擎级一买一卖对账:佣金/印花税/滑点手工复算,与 trades.cost 和现金变化对账到分
"""
import math

import numpy as np
import pandas as pd
import pytest

from sinan.backtest.costs import cash_delta, trade_cost
from sinan.backtest.engine import run_backtest
from sinan.backtest.execution_model import exec_price, fillable, prepare_market
from sinan.config import load_rules
from sinan.universe.instruments import resolve_rule

from tests.test_engine import DAYS, cfg_for, make_bars, seed_store, settings_with

RULES = load_rules()


# --------------------------------------------------------------------------
# costs:佣金(双边)+ 印花税(仅卖出);滑点不在此计(已并入执行价)
# --------------------------------------------------------------------------
def test_trade_cost_and_cash_delta():
    stock = resolve_rule("600519", "stock", RULES, name="贵州茅台")
    gross = 100_000.0
    assert trade_cost("buy", gross, stock) == pytest.approx(gross * stock.commission)
    assert trade_cost("sell", gross, stock) == pytest.approx(
        gross * (stock.commission + stock.stamp_tax_sell))
    assert cash_delta("buy", gross, stock) == pytest.approx(
        -(gross + gross * stock.commission))
    assert cash_delta("sell", gross, stock) == pytest.approx(
        gross - gross * (stock.commission + stock.stamp_tax_sell))

    etf = resolve_rule("510300", "etf", RULES)
    assert trade_cost("sell", gross, etf) == pytest.approx(gross * etf.commission)  # 无印花税

    with pytest.raises(ValueError):
        trade_cost("hold", gross, stock)


# --------------------------------------------------------------------------
# execution_model:执行价 = price_mode 基准价 ± 滑点(不复权口径)
# --------------------------------------------------------------------------
def test_exec_price_modes():
    etf = resolve_rule("510300", "etf", RULES)
    row = pd.Series({"open_raw": 9.5, "close_raw": 10.0})
    assert exec_price(row, etf, "buy", "close") == pytest.approx(10.0 * (1 + etf.slippage))
    assert exec_price(row, etf, "sell", "close") == pytest.approx(10.0 * (1 - etf.slippage))
    assert exec_price(row, etf, "buy", "open") == pytest.approx(9.5 * (1 + etf.slippage))
    assert exec_price(row, etf, "sell", "open") == pytest.approx(9.5 * (1 - etf.slippage))


def test_prepare_market_limit_inference_stock(tmp_path):
    """股票:缺失涨跌停用 round(pre_close×(1±10%), 2) 推算;首日无 pre_close 不设限。"""
    rule = resolve_rule("600519", "stock", RULES)
    store = seed_store(tmp_path, [make_bars("600519", DAYS[:3], [10.0, 10.5, 11.0])],
                       sec_type="stock")
    m = prepare_market(store.read_bars(symbols=["600519"], adjust=True), rule)
    assert np.isnan(m["up_limit"].iloc[0]) and np.isnan(m["down_limit"].iloc[0])
    assert m["up_limit"].iloc[1] == pytest.approx(11.0)    # round(10.0×1.1, 2)
    assert m["down_limit"].iloc[1] == pytest.approx(9.0)
    assert m["up_limit"].iloc[2] == pytest.approx(11.55)   # round(10.5×1.1, 2)


def test_prepare_market_limit_inference_etf(tmp_path):
    """ETF:3 位小数;显式给出的 up_limit 优先于推算值。"""
    rule = resolve_rule("510300", "etf", RULES)
    up = [np.nan, 1.400, np.nan]                            # d1 显式涨停价
    store = seed_store(tmp_path,
                       [make_bars("510300", DAYS[:3], [1.234, 1.300, 1.310], up_limit=up)])
    m = prepare_market(store.read_bars(symbols=["510300"], adjust=True), rule)
    assert m["up_limit"].iloc[1] == pytest.approx(1.400)    # 显式列保留,不被覆盖
    assert m["down_limit"].iloc[1] == pytest.approx(1.111)  # round(1.234×0.9, 3)
    assert m["up_limit"].iloc[2] == pytest.approx(1.430)    # round(1.300×1.1, 3)


def test_fillable():
    assert not fillable("buy", 11.0, 11.0, 9.0)            # 触及涨停买不进
    assert fillable("buy", 10.5, 11.0, 9.0)
    assert not fillable("sell", 9.0, 11.0, 9.0)            # 触及跌停卖不出
    assert fillable("sell", 9.5, 11.0, 9.0)
    assert fillable("sell", 9.0, 11.0, 9.0, force=True)    # 强平无视跌停
    assert fillable("buy", 99.0, np.nan, np.nan)           # 无涨跌停约束(首日)


# --------------------------------------------------------------------------
# 引擎级对账:一买一卖,佣金/印花税/滑点逐项手工复算,对账到分
# --------------------------------------------------------------------------
def test_fee_reconciliation_stock(tmp_path):
    A = "600519"
    rule = resolve_rule(A, "stock", RULES)
    assert rule.stamp_tax_sell > 0                          # 股票才有印花税,测得其所
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8)], sec_type="stock")
    d = [x.strftime("%Y-%m-%d") for x in DAYS]
    cfg = cfg_for([A], "eng_plan", sec_type="stock",
                  params={"plan": {d[0]: {A: 0.3}, d[2]: {A: 0.0}}})
    res = run_backtest(store, cfg, settings_with(band=0.001), initial_capital=1e6)

    # 手工复算:d0 空仓 equity=1e6 → 目标金额 30 万;d1 买入、d3 卖出
    px_b = 10.0 * (1 + rule.slippage)                       # 滑点计入买入执行价
    n = math.floor(300_000.0 / px_b / rule.lot_size) * rule.lot_size
    gross_b = n * px_b
    fee_b = gross_b * rule.commission                       # 买入只有佣金
    px_s = 10.0 * (1 - rule.slippage)
    gross_s = n * px_s
    fee_s = gross_s * (rule.commission + rule.stamp_tax_sell)   # 卖出佣金+印花税

    tr = res.trades
    assert list(tr["side"]) == ["buy", "sell"]
    assert list(tr["date"]) == [DAYS[1], DAYS[3]]
    assert tr["qty"].iloc[0] == n and tr["qty"].iloc[1] == pytest.approx(n)
    assert tr["price"].iloc[0] == pytest.approx(px_b, abs=1e-12)
    assert tr["price"].iloc[1] == pytest.approx(px_s, abs=1e-12)
    assert tr["cost"].iloc[0] == pytest.approx(fee_b, abs=0.005)    # 对账到分
    assert tr["cost"].iloc[1] == pytest.approx(fee_s, abs=0.005)

    # 现金账:期末全现金,净值 × 本金 == 手工现金流终值(到分)
    cash_end = 1e6 - gross_b - fee_b + gross_s - fee_s
    assert res.nav.iloc[-1] * 1e6 == pytest.approx(cash_end, abs=0.005)
    # trades 流水自身也能重演出同一现金终值
    walk = 1e6 - (tr["qty"].iloc[0] * tr["price"].iloc[0] + tr["cost"].iloc[0]) \
        + (tr["qty"].iloc[1] * tr["price"].iloc[1] - tr["cost"].iloc[1])
    assert walk == pytest.approx(cash_end, abs=0.005)
