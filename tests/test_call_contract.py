"""调用契约测试:ctx 列可见性对称、call_strategy 调用约定、计划锚点自声明。

这三条曾各自靠注释维系,且都属于"漏一处就静默分叉"的类型(lookback
漏传已真实发生过一次)。测试锁的是**契约本身**,不是某个策略的行为:
任何新回测引擎/新实盘 runner 只要走 call_strategy 与 SignalContext,
就自动继承同一份语义。
"""
import pandas as pd
import pytest

from sinan.config import StrategyCfg
from sinan.signal.base import (SIG_COLS, SignalContext, call_strategy, register,
                               resolve_params, strategy_meta)


def _frame(extra: dict | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05", periods=5, freq="B")
    df = pd.DataFrame({c: 1.0 for c in SIG_COLS}, index=idx)
    for k, v in (extra or {}).items():
        df[k] = v
    return df


def _ctx(data) -> SignalContext:
    return SignalContext(today=pd.Timestamp("2026-01-09"), data=data,
                         positions={}, total_asset=1e6, rules={}, universe=["A"])


# ------------------------------------------------------- 列可见性(物理保证)
def test_ctx_strips_execution_columns():
    """执行细节列不进 ctx——实盘侧喂全列 DataFrame 也一样被裁。

    回测 engine 预裁、实盘 run_signal 直接喂 store.read_bars 全列;
    裁剪必须在 SignalContext 里做,否则就是"回测裁、实盘不裁"的半边保证。
    """
    full = _frame({"close_raw": 9.0, "adj_factor": 1.2, "up_limit": 2.0,
                   "symbol": "A", "sec_type": "etf"})
    bars = _ctx({"A": full}).bars("A", 10)
    assert list(bars.columns) == SIG_COLS
    for leaked in ("close_raw", "adj_factor", "up_limit", "symbol", "sec_type"):
        assert leaked not in bars.columns


def test_ctx_columns_identical_across_engines():
    """同一标的,回测侧(已预裁)与实盘侧(全列)得到完全相同的 ctx 视图。"""
    pre_trimmed = _frame()                                   # 引擎 sig_data 形态
    full = _frame({"close_raw": 9.0, "adj_factor": 1.2})     # run_signal 形态
    bt = _ctx({"A": pre_trimmed}).bars("A", 10)
    live = _ctx({"A": full}).bars("A", 10)
    assert list(bt.columns) == list(live.columns)
    pd.testing.assert_frame_equal(bt, live)


def test_ctx_tolerates_missing_column():
    """缺列容忍:新浪源无 amount,不能因此抛 KeyError 挡住当天出信号。"""
    df = _frame().drop(columns=["amount"])
    bars = _ctx({"A": df}).bars("A", 10)
    assert list(bars.columns) == [c for c in SIG_COLS if c != "amount"]


def test_ctx_prefiltered_frame_not_copied():
    """已是标准列集则原样透传——引擎逐日重建 ctx 不能因此每天复制一遍。"""
    df = _frame()
    assert _ctx({"A": df})._data["A"] is df


# ------------------------------------------------------- call_strategy 约定
def test_call_strategy_always_passes_lookback():
    """lookback 必由调用约定传入:漏传曾造成回测/实盘静默分叉。"""
    seen = {}

    @register("_t_lookback")
    def _s(ctx, *, n=1, lookback=750, **_):
        seen.update(n=n, lookback=lookback)
        return {"A": 0.5}

    cfg = StrategyCfg(name="t", strategy="_t_lookback", universe=["A"],
                      lookback=321, params={"n": 7})
    out = call_strategy(cfg, _ctx({"A": _frame()}))
    assert seen == {"n": 7, "lookback": 321}
    assert out == {"A": 0.5}


def test_call_strategy_normalizes_none():
    """策略返回 None 视为空仓,调用方不必各自 `or {}`。"""

    @register("_t_none")
    def _s(ctx, **_):
        return None

    cfg = StrategyCfg(name="t", strategy="_t_none", universe=["A"])
    assert call_strategy(cfg, _ctx({"A": _frame()})) == {}


# ------------------------------------------------------- 计划锚点自声明
def test_window_anchored_param_rewritten_only_in_backtest():
    """回测把计划锚点改写为窗口首日;实盘(不传 window_start)用配置值。

    这条语义原先以 `if cfg.strategy == "dca"` 硬编码在引擎里——每个新
    回测引擎都得复刻才不分叉。现由策略在 @register 自声明。
    """
    got = []

    @register("_t_plan", window_anchored_params=("start",))
    def _s(ctx, *, start="2020-01-01", **_):
        got.append(start)
        return {}

    cfg = StrategyCfg(name="t", strategy="_t_plan", universe=["A"],
                      params={"start": "2020-01-01"})
    ctx = _ctx({"A": _frame()})
    call_strategy(cfg, ctx, window_start=pd.Timestamp("2015-01-05"))
    call_strategy(cfg, ctx)                       # 实盘:配置值原样生效
    assert got == ["2015-01-05", "2020-01-01"]


def test_window_anchor_ignores_undeclared_and_absent():
    """未声明该参数的策略不受影响;声明了但配置没写也不凭空注入。"""
    cfg_plain = StrategyCfg(name="t", strategy="donchian", universe=["A"],
                            params={"start": "2020-01-01"})
    assert resolve_params(cfg_plain, window_start=pd.Timestamp("2015-01-05")
                          )["start"] == "2020-01-01"
    cfg_dca = StrategyCfg(name="t", strategy="dca", universe=["A"], params={})
    assert "start" not in resolve_params(cfg_dca,
                                         window_start=pd.Timestamp("2015-01-05"))


def test_dca_declares_start_as_window_anchor():
    """内置 dca 的声明存在——引擎不再按策略名硬编码这条特例。"""
    assert strategy_meta("dca")["window_anchored_params"] == ("start",)
    assert strategy_meta("donchian")["window_anchored_params"] == ()


def test_register_still_rejects_duplicates():
    with pytest.raises(ValueError, match="重名"):
        register("dca")(lambda ctx, **_: {})
