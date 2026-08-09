"""
数据源抽象接口(方案 §5.2)。

akshare / tushare 的接口名与返回字段随版本变化较快,本层把这种不稳定
隔离在各自的适配器单文件里:上层(update.py)只面对 DataSource 的
四个方法与 store 标准列——数据源怎么换、字段怎么改,都不出 sources/ 目录。
update.run_daily 按 [主源, 备源, ...] 顺序逐调用尝试,任一方法抛
DataSourceError 即自动切下一个源。

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
