"""
Task 4 唐奇安策略测试:全部用合成数据,数值手工可验算。

场景基线 _scenario_closes():
  day 0..79   缓降横盘 close = 10 − 0.001·i
              (严格递减 → close[i] 永远 < 55 日 rolling max,横盘期不会误触发入场)
  day 80      跳空突破 close = 11.0(he[79] = close[25] = 9.975 → 触发入场)
  day 81..110 每日 +0.1 单边上行至 14.0(持续创新高,吊灯线始终在脚下,不出场)

OHLC 构造:high = close+0.2, low = close−0.2 →
  横盘期 TR = max(0.4, |±0.2∓0.001|) ≡ 0.4,故 ATR(20)[79] = 0.4 精确成立。

入场日(day 80)锁定权重可手算:
  stop_frac = m·ATR[79]/close[80] = 3×0.4/11.0
  w = min(cap, x_risk/stop_frac) = 0.025×11.0/1.2 ≈ 0.229167

出场验算(接在 day 110 = 14.0 之后):
  day 111 close=12.5:吊灯线 = hh − m·ATR = 14.0 − 1.2 = 12.8 → atr 模式出场;
          20 日低点 le[110] = close[91] = 12.1 → donchian 模式仍持有(模式判别)
  day 112 close=12.0:le[111] = close[92] = 12.2 → donchian 模式出场
"""
import numpy as np
import pandas as pd
import pytest

from sinan.config import ROOT, load_strategy
from sinan.signal.base import SignalContext, get_strategy
from sinan.universe.cb_terms import EVT_REDEEM_ANNOUNCE, CBEvent, CBTerms

# 与场景基线对应的手算常量
X_RISK, ATR_M, ATR0, C_ENTRY = 0.025, 3.0, 0.4, 11.0
W_ENTRY = X_RISK / (ATR_M * ATR0 / C_ENTRY)          # ≈ 0.229167,入场日锁定权重

I_BREAK = 80      # 突破日下标
I_PEAK = 110      # 上行末日(close = 14.0)


def _scenario_closes(extra=()):
    flat = 10.0 - 0.001 * np.arange(80)              # 缓降横盘
    up = 11.0 + 0.1 * np.arange(31)                  # day80 突破 11.0 → day110 = 14.0
    return np.concatenate([flat, up, np.asarray(extra, dtype=float)])


def _df(closes, start="2020-01-01"):
    c = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"open": c, "high": c + 0.2, "low": c - 0.2,
                         "close": c, "volume": 1e6, "amount": c * 1e6}, index=idx)


def _ctx(data, today, cb=None):
    return SignalContext(today=today, data=data, positions={}, total_asset=1e6,
                         rules={}, universe=list(data), cb_terms=cb)


@pytest.fixture(scope="module")
def fn():
    # 经注册表取用,同时验证 strategies/__init__.py 的注册链路
    return get_strategy("donchian")


# ---------------------------------------------------------------- 入场与定仓
def test_entry_weight_locked(fn):
    df = _df(_scenario_closes())
    # 突破日当天:ctx 输出即为入场锁定权重(引擎 T+1 执行)
    out = fn(_ctx({"510300": df}, today=df.index[I_BREAK]))
    assert out == {"510300": pytest.approx(W_ENTRY)}
    # 持有期间(day 110):ATR 已随行情变化,但权重仍是入场时刻锁定值
    out2 = fn(_ctx({"510300": df}, today=df.index[I_PEAK]))
    assert out2 == {"510300": pytest.approx(W_ENTRY)}


def test_flat_before_breakout(fn):
    df = _df(_scenario_closes())
    # 突破前一日:空仓,只返回权重 > 0 的标的 → 空 dict
    assert fn(_ctx({"510300": df}, today=df.index[I_BREAK - 1])) == {}


def test_window_guard(fn):
    df = _df(_scenario_closes())
    ctx = _ctx({"510300": df}, today=df.index[I_BREAK])
    # 窗口不足 n_entry + atr_n = 75 根 → 强制 0(即使当日恰是突破日)
    assert fn(ctx, lookback=74) == {}
    # 恰好 75 根:入场日在窗口内,指标齐备 → 正常给出锁定权重
    assert fn(ctx, lookback=75) == {"510300": pytest.approx(W_ENTRY)}


# ---------------------------------------------------------------- 出场
def test_chandelier_exit(fn):
    # day 111 急跌至 12.5,跌破吊灯线 14.0 − 3×0.4 = 12.8 → 权重归 0
    df = _df(_scenario_closes(extra=[12.5]))
    assert fn(_ctx({"510300": df}, today=df.index[I_PEAK])) == \
        {"510300": pytest.approx(W_ENTRY)}          # 急跌前一日仍持有
    assert fn(_ctx({"510300": df}, today=df.index[I_PEAK + 1])) == {}


def test_donchian_exit_mode(fn):
    df = _df(_scenario_closes(extra=[12.5, 12.0]))
    d111, d112 = df.index[I_PEAK + 1], df.index[I_PEAK + 2]
    # 同一份数据判别两种出场:12.5 已破吊灯线(12.8)但未破 20 日低点(12.1)
    assert fn(_ctx({"510300": df}, today=d111)) == {}                       # atr 出场
    out = fn(_ctx({"510300": df}, today=d111), exit_mode="donchian")
    assert out == {"510300": pytest.approx(W_ENTRY)}                        # donchian 仍持有
    # day 112 close=12.0 ≤ le[111]=12.2 → donchian 也出场
    assert fn(_ctx({"510300": df}, today=d112), exit_mode="donchian") == {}


# ---------------------------------------------------------------- 无未来函数
def test_no_lookahead(fn):
    base = _scenario_closes()
    today = _df(base).index[I_PEAK]
    outs = []
    for extra in ([], [100.0], [1.0]):               # T 日之后分别:无数据/暴涨/暴跌
        df = _df(np.append(base, extra))
        outs.append(fn(_ctx({"510300": df}, today=today)))
    assert outs[0] == outs[1] == outs[2]             # T 日输出与未来数据完全无关
    assert outs[0] == {"510300": pytest.approx(W_ENTRY)}


# ---------------------------------------------------------------- 可转债强赎
def test_redeem_announce_forces_zero(fn):
    df = _df(_scenario_closes())
    ann = df.index[I_PEAK]                           # 强赎公告日
    cb = {"113001": CBTerms(symbol="113001",
                            events=(CBEvent(date=ann, event=EVT_REDEEM_ANNOUNCE),))}
    # 公告日(含)起:权重强制 0,不接飞刀
    assert fn(_ctx({"113001": df}, today=ann, cb=cb)) == {}
    # 公告日前一天:事件尚未发生,完全不受影响
    out = fn(_ctx({"113001": df}, today=df.index[I_PEAK - 1], cb=cb))
    assert out == {"113001": pytest.approx(W_ENTRY)}


# ---------------------------------------------------------------- 组合权重缩放
def test_total_weight_scaling(fn):
    df = _df(_scenario_closes())
    data = {"510300": df, "510500": df.copy()}
    ctx = _ctx(data, today=df.index[I_PEAK])
    # 默认参数:Σw = 2×0.229 < 1 → 不缩放
    out = fn(ctx)
    assert out == {"510300": pytest.approx(W_ENTRY), "510500": pytest.approx(W_ENTRY)}
    # 调大 x_risk 使每标的触 cap=1.0 → Σw = 2 > 1 → 等比缩至 0.5/0.5,现金 = 0
    out2 = fn(ctx, x_risk=0.4)
    assert out2 == {"510300": pytest.approx(0.5), "510500": pytest.approx(0.5)}
    assert sum(out2.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------- 策略配置
def test_strategy_yaml():
    cfg = load_strategy(ROOT / "config" / "strategies" / "donchian_etf.yaml")
    assert (cfg.name, cfg.strategy, cfg.sec_type) == ("donchian_etf", "donchian", "etf")
    assert cfg.lookback == 750
    # 跨资产七篮子,与老脚本 etf_trend_backtest.py 的 ETFS 对齐
    assert cfg.universe == ["510300", "510500", "159915", "512880",
                            "518880", "513100", "511010"]
    # params 显式写全策略参数(lookback 走 StrategyCfg 顶层字段,不重复入 params,
    # 避免引擎 fn(ctx, lookback=cfg.lookback, **cfg.params) 关键字冲突);
    # x_risk/cap 为老脚本"等分策略槽"的组合级等价:cap=1/7, x_risk=(0.35/8)/7
    assert cfg.params == {"n_entry": 55, "n_exit": 20, "atr_n": 20, "atr_m": 3.0,
                          "x_risk": 0.00625, "cap": 0.143, "exit_mode": "atr"}
