"""
Task 5 快照测试(方案 §9.3):固定合成 3 标的行情 + 哑趋势策略跑全程,
逐日 nav 与 golden 文件(tests/fixtures/snapshot_nav.csv)逐位一致。

golden 由首跑生成并锁定;此后任何引擎重构导致 nav 变化都会在此暴露 ——
若是有意的口径变更,须删除 golden 重新生成并在提交说明中记录原因。
"""
from pathlib import Path

import numpy as np
import pandas as pd

from sinan.backtest.engine import run_backtest
from sinan.backtest.result import TRADE_COLS
from sinan.signal.base import register

from tests.test_engine import cfg_for, make_bars, seed_store, settings_with

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "snapshot_nav.csv"
SNAP_DAYS = pd.date_range("2023-01-02", periods=120, freq="B")
SYMS = ["510300", "510500", "159915"]


@register("snap_trend")
def _snap_trend(ctx, n=20, w=0.3, lookback=60):
    """收盘价在 n 日均线上方 → 目标权重 w,否则 0 —— 确定性趋势哑策略。"""
    out = {}
    for s in ctx.universe():
        b = ctx.bars(s, n)
        out[s] = w if len(b) >= n and b["close"].iloc[-1] > b["close"].mean() else 0.0
    return out


def _make_frames():
    """固定 seed 的几何随机游走,三标的行情逐次生成 —— 任何一次重跑逐位相同。"""
    rng = np.random.default_rng(20260804)
    frames = []
    for i, s in enumerate(SYMS):
        ret = rng.normal(0.0004, 0.018, len(SNAP_DAYS))
        close = np.round(10.0 * (1 + i) * np.exp(np.cumsum(ret)), 3)
        open_ = np.round(close * (1 + rng.normal(0.0, 0.003, len(SNAP_DAYS))), 3)
        frames.append(make_bars(s, SNAP_DAYS, close, open_=open_))
    return frames


def test_snapshot_nav(tmp_path):
    store = seed_store(tmp_path, _make_frames(), calendar=SNAP_DAYS)
    cfg = cfg_for(SYMS, "snap_trend", params={"n": 20, "w": 0.3})
    res = run_backtest(store, cfg, settings_with(band=0.02), initial_capital=1_000_000.0)

    assert len(res.trades) > 0                     # 策略确实交易过,快照非平凡
    assert list(res.trades.columns) == TRADE_COLS
    assert len(res.nav) == len(SNAP_DAYS)

    got = pd.DataFrame({"date": res.nav.index.strftime("%Y-%m-%d"),
                        "nav": res.nav.to_numpy()})
    if not GOLDEN.exists():                        # 首跑生成 golden,此后锁定
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        got.to_csv(GOLDEN, index=False)

    # float_precision="round_trip":pandas 默认快速解析器有 1 ulp 误差,
    # 严格逐位比较必须用可精确往返的解析器
    gold = pd.read_csv(GOLDEN, float_precision="round_trip")
    assert list(got["date"]) == list(gold["date"])
    # 逐位一致:to_csv 的最短往返表示 + round_trip 解析可精确还原 float64
    assert np.array_equal(got["nav"].to_numpy(), gold["nav"].to_numpy())
