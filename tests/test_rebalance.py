"""等权再平衡基准策略:周期首日判定/维持日零调仓/首日建仓。"""
import numpy as np
import pandas as pd

from sinan.config import load_rules
from sinan.signal.base import SignalContext, get_strategy
from sinan.universe.instruments import resolve_rule


def _ctx(dates, today, positions):
    c = np.linspace(10, 11, len(dates))
    df = pd.DataFrame({"open": c, "high": c + .1, "low": c - .1, "close": c,
                       "volume": 1e6, "amount": c * 1e6},
                      index=pd.DatetimeIndex(dates))
    rules = {"510300": resolve_rule("510300", "etf", load_rules())}
    return SignalContext(today=pd.Timestamp(today), data={"510300": df},
                         positions=positions, total_asset=1e6, rules=rules,
                         universe=["510300"], cb_terms=None)


def test_first_day_enters():
    ctx = _ctx(["2024-01-04"], "2024-01-04", positions={})
    assert get_strategy("rebalance")(ctx, freq="M") == {"510300": 1.0}


def test_maintains_within_period():
    """同一周期内:目标 = 现状(引擎零委托),即使权重已漂移。"""
    ctx = _ctx(["2024-01-04", "2024-01-05"], "2024-01-05",
               positions={"510300": 0.87})
    assert get_strategy("rebalance")(ctx, freq="M") == {"510300": 0.87}


def test_rebalances_on_period_first_trading_day():
    """跨月第一个交易日(前一根 bar 属上月)→ 拉回目标权重。"""
    ctx = _ctx(["2024-01-30", "2024-01-31", "2024-02-01"], "2024-02-01",
               positions={"510300": 0.87})
    assert get_strategy("rebalance")(ctx, freq="M") == {"510300": 1.0}
    # 季度口径下 2 月不是新周期 → 维持
    assert get_strategy("rebalance")(ctx, freq="Q") == {"510300": 0.87}


def test_never_mode_only_enters_once():
    ctx = _ctx(["2024-01-30", "2024-02-01"], "2024-02-01",
               positions={"510300": 0.6})
    assert get_strategy("rebalance")(ctx, freq="N") == {"510300": 0.6}
