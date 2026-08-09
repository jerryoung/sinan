"""v3 规则化选样 pick_basket 的单元测试:配额/流动性排序/相关剔除/历史门槛。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from select_universe_v3 import pick_basket   # noqa: E402


def _series(ret, amount, n=300, seed=None):
    """由日收益序列构造 close/amount 帧。"""
    r = np.asarray(ret, dtype=float)[:n]
    idx = pd.date_range("2019-01-01", periods=len(r), freq="B")
    close = 10 * np.cumprod(1 + r)
    return pd.DataFrame({"close": close, "amount": amount}, index=idx)


def _make():
    rng = np.random.default_rng(7)
    base = rng.normal(0, 0.01, 300)
    noise = rng.normal(0, 0.01, 300)
    return {
        "A1": _series(base, amount=9e8),                 # 类A,流动性最高
        "A2": _series(base + 0.0005 * noise, 8e8),       # 类A,与 A1 高相关(ρ≈1)
        "A3": _series(noise, 7e8),                       # 类A,与 A1 低相关
        "B1": _series(rng.normal(0, 0.01, 300), 5e8),
        "B2": _series(rng.normal(0, 0.01, 100), 6e8),    # 类B,历史不足 200 根
    }


CANDS = [("A1", "甲"), ("A2", "甲"), ("A3", "甲"), ("B1", "乙"), ("B2", "乙")]


def test_quota_liquidity_and_corr():
    """配额约束下:类内按流动性取头部,高相关候选被跳过、低相关递补。"""
    picked = pick_basket(CANDS, _make(), quota={"甲": 2, "乙": 1})
    assert picked == ["A1", "A3", "B1"]   # A2 因 ρ>0.75 被剔,A3 递补;B2 历史不足


def test_min_days_gate():
    """历史不足 min_days 的候选没有参赛资格(即使流动性最高)。"""
    picked = pick_basket(CANDS, _make(), quota={"乙": 2})
    assert picked == ["B1"]               # B2 amount 更大但仅 100 根 → 出局


def test_quota_not_exceeded():
    picked = pick_basket(CANDS, _make(), quota={"甲": 1})
    assert picked == ["A1"]               # 只取类内流动性第一


def test_corr_max_relaxed():
    """放宽相关阈值到 1.0 → 纯流动性配额,A2 不再被剔。"""
    picked = pick_basket(CANDS, _make(), quota={"甲": 2}, corr_max=1.01)
    assert picked == ["A1", "A2"]
