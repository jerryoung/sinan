"""定投策略测试:日程/权重数学/现金耗尽/下跌加码/无未来函数/YAML。"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.config import load_rules, load_strategy
from trend.signal.base import SignalContext, get_strategy
from trend.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]


def _df(closes, start="2024-01-01", spread=0.05):
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(c), freq="B")
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread,
                         "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)


def _ctx(frames, today=None):
    rules = {s: resolve_rule(s, "etf", load_rules()) for s in frames}
    any_idx = next(iter(frames.values())).index
    return SignalContext(today=today or any_idx[-1], data=frames,
                         positions={}, total_asset=1e5, rules=rules,
                         universe=list(frames), cb_terms=None)


P = dict(start="2024-01-01", freq="M", amount=4000.0, capital=100000.0,
         dip_rule="none", ma_n=250)
FN = get_strategy("dca")


def test_first_buy_weight_math():
    """首期 4000 元买入价格 10 → 权重 = 4000/100000 = 4%(价格未变)。"""
    df = _df([10.0] * 10)
    w = FN(_ctx(df_map := {"510300": df}, today=df.index[0]), **P, lookback=750)
    assert w["510300"] == pytest.approx(0.04)


def test_price_move_changes_weight_not_units():
    """份额锁定:价格 10→12,权重 = 4800/(96000+4800)。"""
    df = _df([10.0] + [10.0, 11.0, 12.0])
    w = FN(_ctx({"510300": df}), **P, lookback=750)
    assert w["510300"] == pytest.approx(4800 / 100800)


def test_monthly_schedule_accumulates():
    """跨月第二期再投:2 月首个交易日后投入总额 8000。"""
    df = _df([10.0] * 25)                      # 2024-01-01 → 2024-02-05 覆盖两月
    w = FN(_ctx({"510300": df}), **P, lookback=750)
    assert w["510300"] == pytest.approx(0.08)  # 价格不变 → 8000/100000


def test_basket_split_and_late_listing():
    """三标的等分;晚上市标的该份额跳过(不补投)。"""
    a = _df([10.0] * 25)
    b = _df([20.0] * 25)
    c = _df([5.0] * 5, start="2024-02-01")     # 2 月才上市 → 第一期缺席
    w = FN(_ctx({"A": a, "B": b, "C": c}), **P, lookback=750)
    # 两期:A/B 各 4000/3×2;C 仅第二期 4000/3
    assert w["A"] == pytest.approx((8000 / 3) / 100000)
    assert w["C"] == pytest.approx((4000 / 3) / 100000)


def test_cash_exhaustion_stops_plan():
    """本金 6000、每期 4000:第二期只花剩余 2000,之后不再投。"""
    df = _df([10.0] * 45)                      # 覆盖 3 个月
    w = FN(_ctx({"510300": df}), **dict(P, capital=6000.0), lookback=750)
    assert w["510300"] == pytest.approx(1.0)   # 现金 0,全部在仓位上


def test_dip_rule_doubles_below_ma():
    """dip2x:价格低于 ma_n 均线 → 当期投入 ×2(用短均线便于构造)。"""
    closes = [10.0] * 20 + [8.0] * 5           # 2月首日价 8 < 10日均线(≈9.2)
    df = _df(closes)
    w0 = FN(_ctx({"510300": df}, today=df.index[-1]), **dict(P, ma_n=10), lookback=750)
    w2 = FN(_ctx({"510300": df}, today=df.index[-1]),
            **dict(P, ma_n=10, dip_rule="dip2x"), lookback=750)
    assert w2["510300"] > w0["510300"] * 1.5   # 至少某期被加码


def test_no_lookahead():
    df = _df([10.0] * 25)
    today = df.index[-1]
    w0 = FN(_ctx({"510300": df}), **P, lookback=750)
    ext = _df([10.0] * 25 + [50.0, 1.0], start="2024-01-01")
    w1 = FN(_ctx({"510300": ext}, today=today), **P, lookback=750)
    assert w0["510300"] == pytest.approx(w1["510300"])


def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "dca_cn_ndx_gold.yaml")
    assert (cfg.name, cfg.strategy) == ("dca_cn_ndx_gold", "dca")
    assert cfg.universe == ["510300", "159941", "518880"]
    assert cfg.capital == 100000
    # dip_rule 2026-08-09 由 none 切换为 dip2x(跌破年线当期加码 2×)
    assert cfg.params == {"start": "2026-08-07", "freq": "M", "amount": 4000,
                          "capital": 100000, "dip_rule": "dip2x", "ma_n": 250}
