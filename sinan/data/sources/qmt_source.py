"""QMT 数据源适配器:经 qmt_shell.qmt_sdk 的 RPC 桥从大 QMT 拉行情。

定位:**交易机本地的高优先级在线源**。QMT 行情由券商终端落地,覆盖
股/基/债全品种,完整性与时效优于免费公网源;但只在能连上 QMT rpc_server
的环境可用——研究机上构造即抛 DataSourceError,build_sources 自动降级
跳过,链上其余源不受影响(与"tushare token 缺失降级"同一语义)。

接口映射(QMT 内置 API 口径,变更时只改本文件):
    日线       C.get_market_data_ex(period="1d");复权因子用后复权收盘 ÷
               不复权收盘推算(与 akshare ETF 同一技巧);转债恒 1
    交易日历   C.get_trading_dates("SH")(毫秒时间戳 → 日期)
    品种主数据 / 转债条款  不提供——抛 DataSourceError 交链上其他源补
      (低频主数据公网源足够,让本源保持薄;抛出即触发 run_daily 逐调用切源)

连接参数:host/port 读默认实盘配置的 qmt.rpc,token 读 ~/.qmt_rpc_token
(机密,严禁入库/日志——异常文本不携带连接细节)。
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from .base import DataSource, DataSourceError, register_source

# get_market_data_ex 返回记录里我们认的列(RPC 序列化后为 list[dict])
_BAR_COLS = ("open", "high", "low", "close", "volume", "amount")


def _qmt_code(symbol: str, sec_type: str) -> str:
    """6 位代码 → QMT 代码(600519 → 600519.SH;与 tushare ts_code 同规则)。"""
    if sec_type == "stock":
        ex = "SH" if symbol[0] in "69" else ("BJ" if symbol[0] in "48" else "SZ")
    elif sec_type == "etf":
        ex = "SH" if symbol[0] == "5" else "SZ"
    else:  # cb:11x 沪、12x 深
        ex = "SH" if symbol.startswith("11") else "SZ"
    return f"{symbol}.{ex}"


def _to_date(t) -> pd.Timestamp:
    """QMT 毫秒时间戳 / 日期串 → Timestamp(防御两种序列化形态)。"""
    if isinstance(t, (int, float)) and t > 1e12:      # epoch 毫秒
        return pd.Timestamp(t, unit="ms").normalize()
    return pd.Timestamp(t).normalize()


@register_source("qmt")
class QmtSource(DataSource):

    def __init__(self) -> None:
        self._qmt = None
        self._connect()

    # ------------------------------------------------------------- 内部
    def _connect(self) -> None:
        try:
            from qmt_shell import qmt_sdk as qmt
        except Exception as e:  # 非仓库环境(如交易机外的精简部署)
            raise DataSourceError(f"qmt_sdk 不可用: {e}") from e
        try:
            qmt.connect_from_settings()
        except Exception as e:
            raise DataSourceError(
                "QMT RPC 连接失败(交易机未开机 / rpc_server 未运行 / "
                f"不在 IP 白名单): {type(e).__name__}") from e
        self._qmt = qmt

    def _md(self, codes: list[str], s: str, e: str,
            dividend_type: str) -> dict:
        """get_market_data_ex 防御性封装:返回 {code: DataFrame(标准列)}。"""
        try:
            raw = self._qmt.C.get_market_data_ex(
                [], codes, period="1d", start_time=s, end_time=e,
                dividend_type=dividend_type)
        except Exception as ex:
            raise DataSourceError(f"qmt get_market_data_ex 失败: {ex}") from ex
        out = {}
        for code, recs in (raw or {}).items():
            df = pd.DataFrame(recs)
            if not len(df):
                continue
            tcol = next((c for c in ("time", "date", "timetag")
                         if c in df.columns), None)
            if tcol is None or "close" not in df.columns:
                continue                       # 序列化形态不符,宁可缺不可错
            df["date"] = [_to_date(t) for t in df[tcol]]
            keep = ["date"] + [c for c in _BAR_COLS if c in df.columns]
            out[code] = df[keep].sort_values("date").reset_index(drop=True)
        return out

    # ------------------------------------------------------------- bars
    def get_bars(self, symbols: list[str], sec_type: str, start, end) -> pd.DataFrame:
        if sec_type not in ("stock", "etf", "cb"):
            raise DataSourceError(f"qmt 不支持的品种类型: {sec_type}")
        s = pd.Timestamp(start).strftime("%Y%m%d")
        e = pd.Timestamp(end).strftime("%Y%m%d")
        code_map = {_qmt_code(str(x), sec_type): str(x) for x in symbols}
        raw = self._md(list(code_map), s, e, "none")
        hfq = {}
        if sec_type != "cb":                  # 转债不复权;股/基用后复权推算因子
            try:
                hfq = self._md(list(code_map), s, e, "back")
            except DataSourceError as ex:     # 因子缺失不整体失败:store 组内 ffill 兜底
                logger.warning(f"qmt 后复权行情拉取失败,adj_factor 交 store ffill: {ex}")
        frames, failed = [], []
        for code, sym in code_map.items():
            df = raw.get(code)
            if df is None or not len(df):
                failed.append(sym)
                continue
            df = df.copy()
            df["symbol"] = sym
            h = hfq.get(code)
            if h is not None and len(h) == len(df):
                f = (pd.to_numeric(h["close"], errors="coerce")
                     / pd.to_numeric(df["close"], errors="coerce"))
                df["adj_factor"] = f.to_numpy()     # NaN 由 store ffill 兜住
            elif sec_type == "cb":
                df["adj_factor"] = 1.0
            # 股票/ETF 拿不到因子时不写该列,store 组内 ffill(永远不是 1.0 的纪律)
            frames.append(df)
        if failed:
            logger.warning(f"qmt get_bars({sec_type}) 无数据 {len(failed)} 只: "
                           + ", ".join(failed[:5]))
        if not frames:
            raise DataSourceError(
                f"qmt get_bars({sec_type}) 全部无数据({len(failed)} 只)")
        return pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------- instruments
    def get_instruments(self, sec_type: str) -> pd.DataFrame:
        raise DataSourceError(
            f"qmt 源不提供品种主数据({sec_type}),请由链上其他源补齐")

    # ---------------------------------------------------------- cb_terms
    def get_cb_terms(self) -> pd.DataFrame:
        raise DataSourceError("qmt 源不提供转债条款,请由链上其他源补齐")

    # ---------------------------------------------------------- calendar
    def get_calendar(self, start, end) -> pd.DatetimeIndex:
        try:
            raw = self._qmt.C.get_trading_dates(
                "SH", start_time=pd.Timestamp(start).strftime("%Y%m%d"),
                end_time=pd.Timestamp(end).strftime("%Y%m%d"))
        except Exception as e:
            raise DataSourceError(f"qmt get_trading_dates 失败: {e}") from e
        if not raw:
            raise DataSourceError("qmt get_trading_dates 返回为空")
        return pd.DatetimeIndex(sorted(_to_date(t) for t in raw))
