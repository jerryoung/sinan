"""
数据源抽象接口(方案 §5.2)。

akshare / tushare 的接口名与返回字段随版本变化较快,本层把这种不稳定
隔离在各自的适配器单文件里:上层(update.py)只面对 DataSource 的
四个方法与 store 标准列——数据源怎么换、字段怎么改,都不出 sources/ 目录。
update.run_daily 按 [主源, 备源, ...] 顺序逐调用尝试,任一方法抛
DataSourceError 即自动切下一个源。

扩展方式(与 signal/base.py 策略注册表同一约定):新增数据源 = 在
sources/ 下加一个 {name}_source.py,适配器类上装饰 @register_source("name");
调用方只按配置(settings.data.sources)里的名字经 build_sources 构建源链,
不 import 任何具体适配器——加源、换源、调顺序都只动配置与 sources/ 目录。

返回列约定(与 trend.data.store 对齐):
    get_bars        symbol, date, open, high, low, close, volume, amount,
                    adj_factor(后复权因子,转债恒 1),
                    可选 pre_close / up_limit / down_limit(供质检涨跌停判定)
                    amount 单位一律为**元**(由适配器归一)
    get_instruments symbol, name, sec_type, exchange, list_date, delist_date, status
    get_cb_terms    symbol, stock_code, conv_price, redeem_status, rating, maturity
    get_calendar    pd.DatetimeIndex(交易日)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataSourceError(RuntimeError):
    """数据源调用失败(网络不可用 / 接口变更 / 字段缺失)。

    异常文本必须携带「哪个源、哪个接口、哪些标的」的上下文,
    但**严禁携带任何凭证**(如 tushare token)。
    """


class DataSource(ABC):
    """日更数据源的统一抽象。

    实现约定:
    - 每个方法内部把第三方接口调用包在 try/except 里,失败抛 DataSourceError,
      网络不可用时也应得到清晰错误而不是裸栈;
    - 该源不覆盖的品种类型返回空 DataFrame(不算失败,不触发切源)。
    """

    name: str = "base"

    @abstractmethod
    def get_bars(self, symbols: list[str], sec_type: str, start, end) -> pd.DataFrame:
        """[start, end] 区间内的日线(store 标准列:不复权价 + adj_factor)。"""

    @abstractmethod
    def get_instruments(self, sec_type: str) -> pd.DataFrame:
        """品种主数据(存续判定依赖 list_date / delist_date / status)。"""

    @abstractmethod
    def get_cb_terms(self) -> pd.DataFrame:
        """可转债条款快照(转股价 / 正股 / 赎回状态等)。"""

    @abstractmethod
    def get_calendar(self, start, end) -> pd.DatetimeIndex:
        """[start, end] 区间内的交易日历。"""


# ---------------------------------------------------------------- 注册表
_SOURCES: dict[str, type[DataSource]] = {}


def register_source(name: str):
    """注册数据源适配器(仿 signal/base.py 的策略注册表)。

    适配器模块按 {name}_source.py 命名,类上装饰本函数即完成注册;
    重复注册同名直接抛错(配置里写错名字应在启动时炸,而不是静默
    落到错误的源上)。
    """
    def deco(cls: type[DataSource]) -> type[DataSource]:
        if name in _SOURCES:
            raise ValueError(f"数据源重名: {name}")
        _SOURCES[name] = cls
        cls.name = name
        return cls
    return deco


def source_names() -> tuple[str, ...]:
    """已注册(含可惰性发现)的数据源名。"""
    return tuple(sorted(_SOURCES))


def create_source(name: str, **kw) -> DataSource:
    """按名创建数据源;适配器模块按 {name}_source 约定惰性 import。

    惰性 import 的意义:akshare/tushare 等第三方库未安装时,只影响对应
    源的构建,不阻碍其余源与整个注册表的加载。
    """
    if name not in _SOURCES:
        import importlib
        try:
            importlib.import_module(f".{name}_source", __package__)
        except ImportError as e:
            raise ValueError(
                f"未知数据源 {name!r}:无适配器模块 sources/{name}_source.py "
                f"(当前已注册: {', '.join(source_names()) or '无'})") from e
    if name not in _SOURCES:
        raise ValueError(
            f"数据源 {name!r} 的适配器模块未调用 register_source 注册")
    return _SOURCES[name](**kw)


def build_sources(names, on_error=None) -> list[DataSource]:
    """按配置顺序构建源链 [主源, 备源, ...]。

    单个源不可用(token 缺失、运行环境无 QMT 等)降级跳过并告警,
    不阻断链路;全部不可用才抛 DataSourceError——与 run_daily
    "任一方法失败切下一个源"的逐调用降级是同一条原则的两端。
    on_error: Callable[[str], None],默认写日志。
    """
    from loguru import logger
    sources, errors = [], []
    for name in names:
        try:
            sources.append(create_source(str(name).strip()))
        except Exception as e:  # noqa: BLE001 单源降级,链路继续
            msg = f"数据源 {name} 不可用,已从链上跳过: {e}"
            errors.append(msg)
            (on_error or logger.warning)(msg)
    if not sources:
        raise DataSourceError(
            f"配置的数据源全部不可用({list(names)}): " + "; ".join(errors))
    return sources
