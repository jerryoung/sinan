"""
信号层核心(方案 §7.1)——整个系统最重要的接口。

    def generate_targets(ctx: SignalContext) -> dict[str, float]

要求:纯函数。不下单、不读实时接口、不知道自己在回测还是实盘。
回测引擎和实盘脚本构造各自的 SignalContext 喂给同一个函数,
这就是回测–实盘一致性的全部秘密。

SignalContext 是唯一的具体实现(不是抽象类):两侧共用同一份
数据截断逻辑,防未来函数在这里物理保证——bars() 永远只返回
截至 today 收盘的数据。
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..universe.cb_terms import CBTerms
from ..universe.instruments import TradingRule

_EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"])


class SignalContext:
    """截至决策时点的全部可见数据(K线、持仓、条款、规则、日历)。"""

    def __init__(
        self,
        today: pd.Timestamp,
        data: dict[str, pd.DataFrame],
        positions: dict[str, float],
        total_asset: float,
        rules: dict[str, TradingRule],
        universe: list[str],
        cb_terms: dict[str, CBTerms] | None = None,
    ) -> None:
        # data[symbol]: 后复权 OHLCV 全历史,DatetimeIndex 升序。
        # 引擎按天构造 ctx 时复用同一份 data,bars() 负责截断——
        # 未来数据在接口层就拿不到,而不是靠策略自觉。
        self.today = pd.Timestamp(today)
        self._data = data
        self.positions = dict(positions)
        self.total_asset = float(total_asset)
        self._rules = rules
        self._universe = list(universe)
        self._cb = cb_terms or {}

    def bars(self, symbol: str, n: int) -> pd.DataFrame:
        """后复权日线,严格截至 today(含 today 收盘),最后 n 根。"""
        df = self._data.get(symbol)
        if df is None or df.empty:
            return _EMPTY.copy()
        return df.loc[: self.today].tail(n)

    def rule(self, symbol: str) -> TradingRule:
        return self._rules[symbol]

    def cb_terms(self, symbol: str) -> CBTerms | None:
        return self._cb.get(symbol)

    def universe(self) -> list[str]:
        return list(self._universe)


# --------------------------------------------------------------------------
# 策略注册表:@register("donchian") → get_strategy("donchian")
# --------------------------------------------------------------------------
Strategy = Callable[..., dict[str, float]]   # (ctx, **params) -> {symbol: weight}

_STRATEGIES: dict[str, Strategy] = {}


def register(name: str):
    def deco(fn: Strategy) -> Strategy:
        if name in _STRATEGIES:
            raise ValueError(f"策略重名: {name}")
        _STRATEGIES[name] = fn
        return fn
    return deco


def get_strategy(name: str) -> Strategy:
    if name not in _STRATEGIES:
        # 惰性导入内置策略包,触发其 @register
        from . import strategies  # noqa: F401
    if name not in _STRATEGIES:
        raise KeyError(f"未注册的策略: {name},已有: {sorted(_STRATEGIES)}")
    return _STRATEGIES[name]
