"""
数据质量检查(方案 §5.2):日更后对当日数据做四类体检。

质检不通过 → update.run_daily 告警并返回失败,**不触发**后续信号计算:
带伤的数据宁可停一天,也不能喂给策略——错误的 K 线对趋势系统的伤害
(假突破入场 / 假跌破止损)远大于晚一天的数据。

四类检查(Issue.kind 取值):
    missing_bar   交易日缺 K 线且无停牌记录(只对当日存续的标的要求)
    price_jump    |涨跌| 超过该标的 limit_pct×1.05 且收盘价不等于涨跌停价。
                  超出涨跌幅限制的成交本不可能,大概率是坏数据或复权价混入;
                  1.05 的宽容系数吸收规则推断误差(如 ST 状态未同步),
                  收盘恰为 up/down_limit 视为真实涨跌停(推断的 limit_pct 偏小)豁免
    ohlc_invalid  high < low,或 close 落在 [low, high] 之外(含 OHLC 出现 NaN)
    duplicate     (symbol, date) 重复行(如同一标的被写进多个 sec_type 分区)

前收盘口径:优先用行内 pre_close(除权日与交易所口径一致);缺失则回溯
最近一根 K 线的收盘价——除权日可能因此误报,属可接受的从严方向。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..calendar import TradeCalendar
from ..config import load_rules
from ..universe.instruments import resolve_rule

# 涨跌幅宽容系数:阈值 = limit_pct × JUMP_TOL
JUMP_TOL = 1.05


def jump_threshold(symbol: str, sec_type: str, rules_cfg: dict | None = None,
                   *, name: str = "") -> float:
    """该标的的单日跳变上限 = limit_pct × JUMP_TOL —— "什么算坏 K 线"的唯一定义。

    拉取侧(入库前逐标的把关)与本模块的日更审计(入库后全量体检)共用同一
    阈值:同一条业务规则不该有两份实现。**必须逐标的算**,不能用一个全局
    常数——科创板 ETF(588/589)涨跌幅是 20%,拿 10% 的阈值去卡会把合法
    行情判成坏数据、拒绝入库,当天直接出不了信号。

    注:全量历史补数(data/ensure.py)另有一套更宽的"仅告警"策略,那是
    有意为之的第三种口径——未复权历史里的分红除权本就是合法跳变,详见其
    模块 docstring;不要"统一"掉它。
    """
    rules_cfg = rules_cfg if rules_cfg is not None else load_rules()
    return resolve_rule(str(symbol), sec_type, rules_cfg, name=name).limit_pct * JUMP_TOL


@dataclass(frozen=True)
class Issue:
    symbol: str
    kind: str      # 'missing_bar' | 'price_jump' | 'ohlc_invalid' | 'duplicate'
    detail: str


@dataclass
class QualityReport:
    ok: bool
    issues: list[Issue] = field(default_factory=list)

    def summary(self) -> str:
        """告警用一行摘要(过长截断)。"""
        if self.ok:
            return "质检通过"
        parts = [f"{i.symbol}[{i.kind}] {i.detail}" for i in self.issues[:10]]
        more = f" …等共 {len(self.issues)} 条" if len(self.issues) > 10 else ""
        return f"{len(self.issues)} 条异常: " + "; ".join(parts) + more


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _dt_col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_datetime(df[name], errors="coerce")
    return pd.Series(pd.NaT, index=df.index)


def alive_instruments(inst: pd.DataFrame, date) -> pd.DataFrame:
    """date 当日存续的标的:已上市(list_date≤date 或未知)且未退市(delist_date≥date 或为空)。

    delist_date 缺失但 status 标记为 D(退市)的同样视为非存续。
    update.py 复用本函数确定「拉当日增量」的标的清单,与质检口径保持一致。
    """
    if inst is None or len(inst) == 0 or "symbol" not in inst.columns:
        return pd.DataFrame(columns=["symbol", "name", "sec_type"])
    date = pd.Timestamp(date)
    ld, dd = _dt_col(inst, "list_date"), _dt_col(inst, "delist_date")
    m = (ld.isna() | (ld <= date)) & (dd.isna() | (dd >= date))
    if "status" in inst.columns:
        m &= ~(dd.isna() & (inst["status"].astype(str).str.upper() == "D"))
    return inst.loc[m].reset_index(drop=True)


def _is_open(store, date: pd.Timestamp, calendar) -> bool:
    """当日是否交易日:优先用传入 calendar,否则读 store;两者皆无则按交易日从严。"""
    if calendar is not None:
        if isinstance(calendar, TradeCalendar):
            return calendar.is_open(date)
        idx = pd.DatetimeIndex(pd.to_datetime(calendar)).normalize()
        return date in set(idx)
    if len(store.read_calendar(start=date, end=date)):
        return True
    # 日历表非空但当日不在 → 非交易日;日历表整个为空 → 无从判断,按交易日处理
    return len(store.read_calendar()) == 0


def _suspended_on(store, date: pd.Timestamp) -> set[str]:
    sus = store.read_suspend()
    if sus is None or len(sus) == 0 or "date" not in sus.columns:
        return set()
    d = pd.to_datetime(sus["date"], errors="coerce")
    return set(sus.loc[d == date, "symbol"].astype(str))


def _at_limit(row) -> bool:
    """收盘价恰为涨停价或跌停价(浮点容差比较)。"""
    for col in ("up_limit", "down_limit"):
        v = getattr(row, col, None)
        if v is not None and pd.notna(v) and math.isclose(
                float(row.close), float(v), rel_tol=1e-6, abs_tol=1e-9):
            return True
    return False


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def check(store, date, calendar=None, rules_cfg: dict | None = None) -> QualityReport:
    """对 date 当日的入库数据做四类体检,返回结构化报告(ok = 零异常)。

    calendar 可传 TradeCalendar 或日期序列;None 时读 store 的 trade_calendar 表。
    """
    date = pd.Timestamp(date).normalize()
    issues: list[Issue] = []
    bars = store.read_bars(start=date, end=date)

    # 1) duplicate:(symbol, date) 重复行
    if len(bars):
        cnt = bars.groupby("symbol").size()
        for sym, n in cnt[cnt > 1].items():
            issues.append(Issue(str(sym), "duplicate", f"(symbol,date) 重复 {n} 行"))

    # 2) ohlc_invalid:high < low,或 close 出界,或价格含 NaN
    for row in bars.itertuples():
        o, h, l, c = row.open, row.high, row.low, row.close
        if any(pd.isna(v) for v in (o, h, l, c)):
            issues.append(Issue(row.symbol, "ohlc_invalid", "OHLC 含 NaN"))
            continue
        if h < l:
            issues.append(Issue(row.symbol, "ohlc_invalid", f"high={h} < low={l}"))
            continue
        if not (l - 1e-9 <= c <= h + 1e-9):
            issues.append(Issue(row.symbol, "ohlc_invalid",
                                f"close={c} 超出 [low={l}, high={h}]"))

    # 3) missing_bar:仅交易日检查;停牌与非存续标的豁免
    if _is_open(store, date, calendar):
        alive = alive_instruments(store.read_instruments(), date)
        present = set(bars["symbol"].astype(str)) if len(bars) else set()
        suspended = _suspended_on(store, date)
        for row in alive.itertuples():
            sym = str(row.symbol)
            if sym not in present and sym not in suspended:
                issues.append(Issue(sym, "missing_bar",
                                    f"{date.date()} 交易日缺 K 线且无停牌记录"))

    # 4) price_jump
    issues += _check_jumps(store, bars, date, rules_cfg)

    return QualityReport(ok=not issues, issues=issues)


def _check_jumps(store, bars: pd.DataFrame, date: pd.Timestamp,
                 rules_cfg: dict | None) -> list[Issue]:
    if not len(bars):
        return []
    rules_cfg = rules_cfg if rules_cfg is not None else load_rules()

    inst = store.read_instruments()
    names = (dict(zip(inst["symbol"].astype(str), inst["name"].astype(str)))
             if len(inst) and {"symbol", "name"} <= set(inst.columns) else {})

    # 行内无 pre_close 的标的:回溯最近一根 K 线的收盘价(取 40 个自然日窗口足矣)
    need_prev = sorted({row.symbol for row in bars.itertuples()
                        if pd.isna(getattr(row, "pre_close", None))})
    prev_close: dict[str, float] = {}
    if need_prev:
        hist = store.read_bars(symbols=need_prev,
                               start=date - pd.Timedelta(days=40),
                               end=date - pd.Timedelta(days=1))
        if len(hist):
            last = hist.sort_values("date").groupby("symbol").tail(1)
            prev_close = dict(zip(last["symbol"].astype(str), last["close"]))

    out: list[Issue] = []
    for row in bars.itertuples():
        pc = row.pre_close if pd.notna(row.pre_close) else prev_close.get(str(row.symbol))
        if pc is None or pd.isna(pc) or pc <= 0 or pd.isna(row.close):
            continue  # 首日无前收盘,无法判定
        sec = getattr(row, "sec_type", None)
        if sec not in rules_cfg:
            continue  # 未知品种类型不做涨跌幅推断
        limit = resolve_rule(str(row.symbol), sec, rules_cfg,
                             name=names.get(str(row.symbol), "")).limit_pct
        pct = abs(float(row.close) / float(pc) - 1.0)
        if pct <= limit * JUMP_TOL or _at_limit(row):
            continue
        out.append(Issue(str(row.symbol), "price_jump",
                         f"|涨跌| {pct:.1%} 超过 {limit:.0%}×{JUMP_TOL} 且非涨跌停价"
                         f"(pre_close={float(pc):g}, close={float(row.close):g})"))
    return out
