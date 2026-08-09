"""
交易日历(方案 §4)。数据来自 store 的 trade_calendar 表。
"""
from __future__ import annotations

import pandas as pd


class TradeCalendar:
    def __init__(self, dates) -> None:
        idx = pd.DatetimeIndex(sorted(pd.DatetimeIndex(dates).unique()))
        if len(idx) == 0:
            raise ValueError("交易日历为空")
        self._dates = idx
        self._set = set(idx)

    @classmethod
    def from_store(cls, store, start=None, end=None) -> "TradeCalendar":
        return cls(store.read_calendar(start=start, end=end))

    def is_open(self, date) -> bool:
        return pd.Timestamp(date).normalize() in self._set

    def sessions(self, start=None, end=None) -> pd.DatetimeIndex:
        d = self._dates
        if start is not None:
            d = d[d >= pd.Timestamp(start)]
        if end is not None:
            d = d[d <= pd.Timestamp(end)]
        return d

    def next_open(self, date, offset: int = 1) -> pd.Timestamp | None:
        """date 之后的第 offset 个交易日;超出日历范围返回 None。"""
        pos = self._dates.searchsorted(pd.Timestamp(date), side="right")
        i = pos + offset - 1
        return self._dates[i] if 0 <= i < len(self._dates) else None

    def prev_open(self, date) -> pd.Timestamp | None:
        """date 之前(不含 date)最近的一个交易日。"""
        pos = self._dates.searchsorted(pd.Timestamp(date), side="left")
        return self._dates[pos - 1] if pos > 0 else None

    def __len__(self) -> int:
        return len(self._dates)
