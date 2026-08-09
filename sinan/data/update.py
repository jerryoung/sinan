"""
每日增量更新编排(方案 §5.2,17:00 cron 调用链的核心)。

流程:交易日历 → 品种主数据 → 存续标的当日 K 线 → 转债条款/强赎事件 → 质检。

两条防线:
  1. 主源失败自动切备源——按逐调用级别切换:某一步主源挂了,备源只要能
     补上这一步即可,没挂的步骤仍走主源(比整体切换损失更小);
  2. 质检不通过 → notify_fn 告警且 ok=False,调度层据此**不触发**后续
     信号计算(方案 §5.2:带伤数据宁可停一天)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from ..universe.cb_terms import EVT_REDEEM_ANNOUNCE
from .quality import QualityReport, alive_instruments, check
from .sources.base import DataSource, DataSourceError

SEC_TYPES = ("stock", "etf", "cb")

# 日历回看窗口(自然日):既覆盖质检回溯前收盘的需求,又保证跨年初始化时表非空
_CAL_LOOKBACK_DAYS = 400

# 赎回状态含"强赎"字样即视为已公告强赎;"不强赎/不提前赎回"是反向声明,必须排除
_REDEEM_NEG = ("不强赎", "不提前赎回", "不赎回")


def _is_redeem_announced(status) -> bool:
    if status is None or (isinstance(status, float) and pd.isna(status)):
        return False
    s = str(status)
    if any(k in s for k in _REDEEM_NEG):
        return False
    return "强赎" in s


@dataclass
class UpdateResult:
    ok: bool
    report: QualityReport | None            # 全源失败 / 非交易日时为 None
    date: pd.Timestamp | None = None
    source_used: dict = field(default_factory=dict)   # {step: 实际使用的源名}
    errors: list[str] = field(default_factory=list)   # 切源过程中吞掉的失败明细


def run_daily(store, sources: list[DataSource], date, notify_fn=None) -> UpdateResult:
    """拉取 date 当日增量 → 入库 → 质检;返回 ok=False 表示不得触发信号计算。

    notify_fn: Callable[[str], None],告警回调(如企业微信推送);None 则只写日志。
    """
    date = pd.Timestamp(date).normalize()
    if not sources:
        raise ValueError("sources 为空:至少提供一个数据源")
    errors: list[str] = []
    used: dict[str, str] = {}

    def _notify(msg: str) -> None:
        logger.error(msg)
        if notify_fn is not None:
            notify_fn(msg)

    def _call(step: str, method: str, *args):
        """按 [主源, 备源, ...] 顺序尝试;全部失败抛 DataSourceError。"""
        last: Exception | None = None
        for src in sources:
            try:
                out = getattr(src, method)(*args)
                used[step] = src.name
                return out
            except Exception as e:
                msg = f"[{src.name}] {method} 失败: {e}"
                logger.warning(msg)
                errors.append(msg)
                last = e
        raise DataSourceError(f"{method} 所有数据源均失败") from last

    try:
        # 1) 交易日历(写入幂等);当日非交易日 → 无事可做
        cal = _call("calendar", "get_calendar",
                    date - pd.Timedelta(days=_CAL_LOOKBACK_DAYS), date)
        if cal is not None and len(cal):
            store.write_calendar(cal)
        if not len(store.read_calendar(start=date, end=date)):
            logger.info(f"{date.date()} 非交易日,跳过更新")
            return UpdateResult(ok=True, report=None, date=date,
                                source_used=used, errors=errors)

        # 2) 品种主数据 + 存续标的当日 K 线
        has_cb = False
        for st in SEC_TYPES:
            inst = _call(f"instruments:{st}", "get_instruments", st)
            if inst is not None and len(inst):
                store.upsert_instruments(inst)
            alive = alive_instruments(store.read_instruments(sec_type=st), date)
            if st == "cb":
                has_cb = len(alive) > 0
            if not len(alive):
                continue  # 该类型无存续标的(源不覆盖或尚未种子化)
            bars = _call(f"bars:{st}", "get_bars",
                         list(alive["symbol"].astype(str)), st, date, date)
            if bars is not None and len(bars):
                store.write_bars(bars, st)

        # 3) 转债条款 + 强赎公告事件(漏掉强赎 = 回测里持有早已退市的债)
        if has_cb:
            terms = _call("cb_terms", "get_cb_terms")
            if terms is not None and len(terms):
                store.upsert_cb_terms(terms)
                _record_redeem_events(store, terms, date)
    except DataSourceError as e:
        _notify(f"日更失败({date.date()}): {e}")
        return UpdateResult(ok=False, report=None, date=date,
                            source_used=used, errors=errors)

    # 4) 质检:不通过 → 告警 + ok=False(调度层据此不触发信号计算)
    report = check(store, date)
    if not report.ok:
        _notify(f"数据质检不通过({date.date()}): {report.summary()}")
    else:
        logger.info(f"日更完成({date.date()}),质检通过")
    return UpdateResult(ok=report.ok, report=report, date=date,
                        source_used=used, errors=errors)


def _record_redeem_events(store, terms: pd.DataFrame, date: pd.Timestamp) -> None:
    """条款快照里首次出现强赎公告字样 → 记 redeem_announce 事件(事件日 = 观察日)。

    幂等:已记过 redeem_announce 的券不重复写(cb_events 主键含 date,
    若按每日观察日直写会天天新增一条,故先查再写)。
    """
    if "redeem_status" not in terms.columns:
        return
    flagged = terms.loc[[_is_redeem_announced(s) for s in terms["redeem_status"]],
                        "symbol"].astype(str).tolist()
    if not flagged:
        return
    known = store.read_cb_events(flagged)
    already: set[str] = set()
    if len(known) and "event" in known.columns:
        already = set(known.loc[known["event"] == EVT_REDEEM_ANNOUNCE,
                                "symbol"].astype(str))
    new = [s for s in flagged if s not in already]
    if new:
        store.write_cb_events(pd.DataFrame({
            "symbol": new, "date": [date] * len(new),
            "event": [EVT_REDEEM_ANNOUNCE] * len(new)}))
        logger.warning(f"记录强赎公告事件({date.date()}): {new}")
