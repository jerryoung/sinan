"""
信号层核心(方案 §7.1)——整个系统最重要的接口。

    def generate_targets(ctx: SignalContext) -> dict[str, float]

要求:纯函数。不下单、不读实时接口、不知道自己在回测还是实盘。
回测引擎和实盘脚本构造各自的 SignalContext 喂给同一个函数,
这就是回测–实盘一致性的全部秘密。

SignalContext 是唯一的具体实现(不是抽象类):两侧共用同一份
数据截断逻辑,防未来函数在这里物理保证——bars() 永远只返回
截至 today 收盘的数据。同理,列可见性也在这里物理保证(SIG_COLS):
执行细节列不进 ctx,两侧策略看到的列集永远一致。

策略调用一律经 call_strategy(cfg, ctx),不要在调用方手写
fn(ctx, **params, lookback=...) —— 漏传 lookback 会造成回测/实盘
静默分叉(历史事故),调用约定必须只有一份实现。
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..universe.cb_terms import CBTerms
from ..universe.instruments import TradingRule

#: 信号层可见列 —— 只暴露后复权 OHLCV。执行细节列(close_raw/adj_factor/
#: 涨跌停价/sec_type…)不进 ctx:策略误用不复权口径会造成回测–实盘偏差,
#: 而回测侧裁、实盘侧不裁的"半边保证"本身就是一种分叉源。
SIG_COLS = ["open", "high", "low", "close", "volume", "amount"]

_EMPTY = pd.DataFrame(columns=SIG_COLS)


def _sig_frame(df: pd.DataFrame) -> pd.DataFrame:
    """裁到 SIG_COLS。缺列容忍(如新浪源无 amount),多列一律丢弃。

    已是标准列集时原样返回——引擎逐日重建 ctx 复用同一份数据,
    这条快路径让物理保证不带来每日复制开销。
    """
    if df is None:
        return df
    cols = [c for c in SIG_COLS if c in df.columns]
    return df if list(df.columns) == cols else df[cols]


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
        # 未来数据在接口层就拿不到,而不是靠策略自觉;列集同理在此裁定,
        # 回测与实盘无论各自喂进来什么,策略看到的永远是同一组列。
        self.today = pd.Timestamp(today)
        self._data = {s: _sig_frame(df) for s, df in data.items()}
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
_META: dict[str, dict] = {}


def register(name: str, *, window_anchored_params: tuple[str, ...] = ()):
    """注册策略。

    window_anchored_params: 声明"该参数是计划锚点,回测中应改用回测窗口
        首日"的参数名(如 dca 的 start)。配置里的值锚定影子/实盘的真实
        计划(必须写死才可复现),而回测问的是"该计划在这段历史上表现
        如何",起点即窗口首日。由策略自己声明,引擎不再按名字硬编码特例
        ——否则每个新回测引擎都得复刻同一条暗规则才不分叉。
    """
    def deco(fn: Strategy) -> Strategy:
        if name in _STRATEGIES:
            raise ValueError(f"策略重名: {name}")
        _STRATEGIES[name] = fn
        _META[name] = {"window_anchored_params": tuple(window_anchored_params)}
        return fn
    return deco


def get_strategy(name: str) -> Strategy:
    if name not in _STRATEGIES:
        # 惰性导入内置策略包,触发其 @register
        from . import strategies  # noqa: F401
    if name not in _STRATEGIES:
        raise KeyError(f"未注册的策略: {name},已有: {sorted(_STRATEGIES)}")
    return _STRATEGIES[name]


def strategy_meta(name: str) -> dict:
    """策略注册元数据(先触发惰性导入,保证注册已发生)。"""
    get_strategy(name)
    return dict(_META.get(name) or {})


def resolve_params(cfg, *, window_start=None) -> dict:
    """策略的实际生效参数:配置参数 + 计划锚点按回测窗口首日改写。

    单独暴露供留痕使用(回测 meta 记录的必须是真正跑的那份参数)。
    """
    params = dict(cfg.params)
    if window_start is not None:
        for key in strategy_meta(cfg.strategy).get("window_anchored_params", ()):
            if key in params:
                params[key] = str(pd.Timestamp(window_start).date())
    return params


def call_strategy(cfg, ctx: SignalContext, *, window_start=None) -> dict[str, float]:
    """按唯一约定调用策略——回测与实盘的共同入口。

    约定 `fn(ctx, **cfg.params, lookback=cfg.lookback)` 只在这里写一次:
    漏传 lookback 曾造成回测/实盘静默分叉,而三个调用点各写一遍就等于
    三次犯同一个错的机会。

    window_start 非 None(回测)时,把策略声明的 window_anchored_params
    改写为窗口首日;实盘不传,配置值原样生效。
    """
    fn = get_strategy(cfg.strategy)
    return fn(ctx, **resolve_params(cfg, window_start=window_start),
              lookback=cfg.lookback) or {}
