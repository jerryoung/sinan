"""cadence 包装器:调仓日透传内层、非调仓日维持现状、D 全透传。"""
import numpy as np
import pandas as pd

from sinan.config import load_rules
from sinan.signal.base import SignalContext, get_strategy, register
from sinan.universe.instruments import resolve_rule

CALLS = []


@register("cadence_inner_probe")
def _probe(ctx, lookback=0, **_):
    CALLS.append(str(ctx.today.date()))
    return {"510300": 0.42}


def _ctx(dates, today, positions):
    c = np.linspace(10, 11, len(dates))
    df = pd.DataFrame({"open": c, "high": c + .1, "low": c - .1, "close": c,
                       "volume": 1e6, "amount": c * 1e6},
                      index=pd.DatetimeIndex(dates))
    rules = {"510300": resolve_rule("510300", "etf", load_rules())}
    return SignalContext(today=pd.Timestamp(today), data={"510300": df},
                         positions=positions, total_asset=1e6, rules=rules,
                         universe=["510300"], cb_terms=None)


INNER = {"strategy": "cadence_inner_probe", "params": {}}


def test_weekly_schedule_day_runs_inner():
    """跨周首个交易日(前一根 bar 属上周)→ 执行内层。"""
    CALLS.clear()
    ctx = _ctx(["2024-01-04", "2024-01-05", "2024-01-08"], "2024-01-08",
               positions={"510300": 0.30})
    out = get_strategy("cadence")(ctx, inner=INNER, freq="W", lookback=750)
    assert out == {"510300": 0.42} and CALLS == ["2024-01-08"]


def test_midweek_maintains_positions():
    """周中:不调用内层,返回当前持仓(零委托)——止损也不在周中执行。"""
    CALLS.clear()
    ctx = _ctx(["2024-01-08", "2024-01-09"], "2024-01-09",
               positions={"510300": 0.30})
    out = get_strategy("cadence")(ctx, inner=INNER, freq="W", lookback=750)
    assert out == {"510300": 0.30} and CALLS == []


def test_monthly_key():
    """月频:2 月首个交易日执行;1 月中旬维持。"""
    CALLS.clear()
    ctx = _ctx(["2024-01-30", "2024-01-31", "2024-02-01"], "2024-02-01",
               positions={})
    assert get_strategy("cadence")(ctx, inner=INNER, freq="M") == {"510300": 0.42}
    CALLS.clear()
    ctx2 = _ctx(["2024-01-15", "2024-01-16"], "2024-01-16", positions={})
    assert get_strategy("cadence")(ctx2, inner=INNER, freq="M") == {}   # 空仓维持=空
    assert CALLS == []


def test_daily_passthrough():
    CALLS.clear()
    ctx = _ctx(["2024-01-09"], "2024-01-09", positions={})
    assert get_strategy("cadence")(ctx, inner=INNER, freq="D") == {"510300": 0.42}
    assert CALLS == ["2024-01-09"]
