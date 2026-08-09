"""combo 组合策略测试:腿加权合成、lookback 传递、全局 Σ>1 缩、YAML。"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sinan.config import load_rules, load_strategy
from sinan.signal.base import SignalContext, get_strategy, register
from sinan.universe.instruments import resolve_rule

ROOT = Path(__file__).resolve().parents[1]


@register("combo_leg_a")
def _leg_a(ctx, w=0.4, lookback=0, **_):
    return {"510300": w, "518880": 0.2}


@register("combo_leg_b")
def _leg_b(ctx, lookback=0, seen=None, **_):
    if seen is not None:
        seen.append(lookback)          # 记录 combo 传入的 lookback
    return {"510300": 0.6}


def _ctx():
    df = pd.DataFrame({"open": [10.0], "high": [10.1], "low": [9.9],
                       "close": [10.0], "volume": [1e6], "amount": [1e7]},
                      index=pd.DatetimeIndex(["2024-01-04"]))
    rules = {s: resolve_rule(s, "etf", load_rules()) for s in ("510300", "518880")}
    return SignalContext(today=df.index[-1], data={"510300": df, "518880": df},
                         positions={}, total_asset=1e6, rules=rules,
                         universe=["510300", "518880"], cb_terms=None)


def test_weighted_merge():
    """腿权重 × 腿内权重逐标的相加:0.5×(0.4+0.6)=0.5 与 0.5×0.2=0.1。"""
    legs = [{"strategy": "combo_leg_a", "weight": 0.5},
            {"strategy": "combo_leg_b", "weight": 0.5}]
    out = get_strategy("combo")(_ctx(), legs=legs, lookback=750)
    assert out["510300"] == pytest.approx(0.5 * 0.4 + 0.5 * 0.6)
    assert out["518880"] == pytest.approx(0.5 * 0.2)


def test_global_rescale():
    """两腿合成 Σ>1 → 全局等比缩到 1(现金非负)。"""
    legs = [{"strategy": "combo_leg_a", "weight": 1.0, "params": {"w": 0.9}},
            {"strategy": "combo_leg_b", "weight": 1.0}]
    out = get_strategy("combo")(_ctx(), legs=legs, lookback=750)
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["510300"] / out["518880"] == pytest.approx((0.9 + 0.6) / 0.2)


def test_leg_lookback_passthrough():
    """腿 params 自带 lookback 优先;缺省用组合层 lookback。"""
    seen = []
    legs = [{"strategy": "combo_leg_b", "weight": 1.0,
             "params": {"seen": seen, "lookback": 123}}]
    get_strategy("combo")(_ctx(), legs=legs, lookback=750)
    seen2 = []
    legs2 = [{"strategy": "combo_leg_b", "weight": 1.0, "params": {"seen": seen2}}]
    get_strategy("combo")(_ctx(), legs=legs2, lookback=750)
    assert seen == [123] and seen2 == [750]


def test_combo_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "combo_turtle_xsmom_x2.yaml")
    assert (cfg.name, cfg.strategy) == ("combo_turtle_xsmom_x2", "combo")
    legs = cfg.params["legs"]
    assert [leg["strategy"] for leg in legs] == ["turtle_s1", "xsmom"]
    assert all(leg["weight"] == 0.5 for leg in legs)
    assert legs[0]["params"]["x_risk"] == 0.00875 and legs[0]["params"]["cap"] == 0.20
    assert legs[1]["params"]["x_risk"] == 0.00875 and legs[1]["params"]["cap"] == 0.20
