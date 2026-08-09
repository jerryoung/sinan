"""
可转债条款与事件(方案 §5.2 / §6)。

强赎公告 → 停止交易日是转债回测正确性的生命线:
漏掉强赎会让回测里"持有"一只早已退市的债。
条款按券存储(强赎触发条件存在变体,不写死统一规则)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# cb_events.event 的标准取值
EVT_REDEEM_ANNOUNCE = "redeem_announce"   # 强赎公告
EVT_LAST_TRADE_DAY = "last_trade_day"     # 停止交易日(最后交易日)
EVT_DELIST = "delist"                     # 摘牌


@dataclass(frozen=True)
class CBEvent:
    date: pd.Timestamp
    event: str


@dataclass(frozen=True)
class CBTerms:
    symbol: str
    stock_code: str | None = None      # 正股代码
    conv_price: float | None = None    # 转股价
    redeem_status: str | None = None   # 数据源原始赎回状态文本
    rating: str | None = None          # 信用评级
    maturity: pd.Timestamp | None = None
    events: tuple[CBEvent, ...] = field(default_factory=tuple)

    def events_until(self, date: pd.Timestamp) -> tuple[CBEvent, ...]:
        """截至 date(含)已发生的事件——调用方防未来函数的唯一入口。"""
        return tuple(e for e in self.events if e.date <= date)

    def has_event(self, event: str, until: pd.Timestamp) -> bool:
        return any(e.event == event for e in self.events_until(until))


def build_cb_terms(terms_df: pd.DataFrame, events_df: pd.DataFrame) -> dict[str, CBTerms]:
    """由 store 的 cb_terms / cb_events 两表构造 {symbol: CBTerms}(events 含全历史,查询时截断)。"""
    ev_map: dict[str, list[CBEvent]] = {}
    if events_df is not None and len(events_df):
        for row in events_df.itertuples():
            ev_map.setdefault(row.symbol, []).append(
                CBEvent(date=pd.Timestamp(row.date), event=row.event))
    out: dict[str, CBTerms] = {}
    for row in terms_df.itertuples():
        sym = row.symbol
        out[sym] = CBTerms(
            symbol=sym,
            stock_code=getattr(row, "stock_code", None),
            conv_price=getattr(row, "conv_price", None),
            redeem_status=getattr(row, "redeem_status", None),
            rating=getattr(row, "rating", None),
            maturity=pd.Timestamp(row.maturity) if getattr(row, "maturity", None) is not None and pd.notna(getattr(row, "maturity")) else None,
            events=tuple(sorted(ev_map.get(sym, []), key=lambda e: e.date)),
        )
    return out
