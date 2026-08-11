"""
akshare 主数据源适配器(方案 §5.2)。

akshare 的接口名与返回字段随版本变化较快,本文件是唯一允许出现 akshare
接口名的地方:全部调用都包在 try/except 里,失败抛 DataSourceError 并携带
「接口 + 标的」上下文,由 update.run_daily 自动切换备源;网络不可用时
也会得到清晰错误而不是裸栈。测试不打网络,本文件只做防御性实现。

接口选型(akshare ≥1.14 口径,接口变更时只改本文件):
    股票日线   stock_zh_a_hist(东财,不复权)+ stock_zh_a_daily(新浪 hfq-factor 复权因子)
    ETF 日线   fund_etf_hist_em(不复权;后复权收盘 ÷ 不复权收盘 = 复权因子)
    转债日线   bond_zh_hs_cov_daily(新浪,返回全历史,取区间切片;转债不复权)
    转债条款   bond_zh_cov(+ bond_cb_redeem_jsl 补赎回状态,失败则缺列)
    交易日历   tool_trade_date_hist_sina
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from .base import DataSource, DataSourceError, register_source

# 东财日线接口(stock_zh_a_hist / fund_etf_hist_em)中文列 → store 标准列
_CN_BARS = {
    "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg",
}


def _ak():
    try:
        import akshare as ak
        return ak
    except Exception as e:  # 未安装或其依赖损坏
        raise DataSourceError(f"akshare 不可用(未安装或依赖损坏): {e}") from e


def _prefix(symbol: str, sec_type: str) -> str:
    """新浪系接口需要 sh/sz 前缀,按 6 位代码首位推断交易所。"""
    if sec_type == "stock":
        mkt = "sh" if symbol[0] in "69" else ("bj" if symbol[0] in "48" else "sz")
    elif sec_type == "etf":
        mkt = "sh" if symbol[0] == "5" else "sz"
    else:  # cb:11x 沪、12x 深
        mkt = "sh" if symbol.startswith("11") else "sz"
    return mkt + symbol


def _yyyymmdd(d) -> str:
    return pd.Timestamp(d).strftime("%Y%m%d")


def fetch_etf_hist_sina(symbol: str) -> pd.DataFrame:
    """新浪 ETF 日线全历史(ensure_bars 全量补数与 sina_feed 影子增量共用)。

    akshare 接口名只允许出现在本文件——两个编排层此前各自 import
    akshare 直调 fund_etf_hist_sina,接口变更要改三处;现收敛到这里。
    返回 date/open/high/low/close/volume(数值化、按日期升序、去空收盘),
    **未复权、无 adj_factor/amount**:复权语义由调用方按 store 存量口径
    处理(全新标的 1.0 起步 / 增量沿用最后因子),OHLC 与跳变质检也留在
    调用方(三处口径有意不同,见 data/ensure.py docstring)。
    """
    ak = _ak()
    pre = "sh" if str(symbol).startswith(("5", "6")) else "sz"
    try:
        raw = ak.fund_etf_hist_sina(symbol=pre + str(symbol))
    except Exception as e:
        raise DataSourceError(
            f"akshare fund_etf_hist_sina({symbol}) 失败: {e}") from e
    if raw is None or not len(raw):
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume"])
    miss = [c for c in ("date", "open", "high", "low", "close")
            if c not in raw.columns]
    if miss:
        raise DataSourceError(
            f"fund_etf_hist_sina 缺列 {miss}(akshare 版本变更?)")
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


@register_source("akshare")
class AkshareSource(DataSource):

    # ------------------------------------------------------------- bars
    def get_bars(self, symbols: list[str], sec_type: str, start, end) -> pd.DataFrame:
        ak = _ak()
        fetch = {"stock": self._stock_bars, "etf": self._etf_bars, "cb": self._cb_bars}
        if sec_type not in fetch:
            raise DataSourceError(f"akshare 不支持的品种类型: {sec_type}")
        frames, failed = [], []
        for sym in symbols:
            try:
                df = fetch[sec_type](ak, str(sym), start, end)
                if df is not None and len(df):
                    frames.append(df)
            except Exception as e:
                failed.append(f"{sym}: {e}")
        if failed and not frames:
            raise DataSourceError(
                f"akshare get_bars({sec_type}) 全部失败({len(failed)} 只),"
                f"示例 → {failed[0]}")
        if failed:
            # 部分失败不切源:缺的标的交给质检 missing_bar 兜底告警
            logger.warning(f"akshare get_bars({sec_type}) 部分失败 {len(failed)} 只: "
                           + "; ".join(failed[:5]))
        if not frames:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close"])
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def _std_cn_bars(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """东财中文列 → 标准列;由涨跌幅反推 pre_close(除权日与交易所口径一致)。"""
        miss = [c for c in ("日期", "开盘", "最高", "最低", "收盘") if c not in raw.columns]
        if miss:
            raise DataSourceError(f"接口返回缺列 {miss}(akshare 版本变更?)")
        df = raw.rename(columns=_CN_BARS)
        df = df[[c for c in df.columns if c in set(_CN_BARS.values())]].copy()
        df["symbol"] = symbol
        df["date"] = pd.to_datetime(df["date"])
        if "pct_chg" in df.columns:
            pct = pd.to_numeric(df["pct_chg"], errors="coerce")
            df["pre_close"] = pd.to_numeric(df["close"], errors="coerce") / (1 + pct / 100.0)
            df = df.drop(columns=["pct_chg"])
        return df.sort_values("date").reset_index(drop=True)

    def _stock_bars(self, ak, sym: str, start, end) -> pd.DataFrame | None:
        raw = ak.stock_zh_a_hist(symbol=sym, period="daily",
                                 start_date=_yyyymmdd(start), end_date=_yyyymmdd(end),
                                 adjust="")
        if raw is None or len(raw) == 0:
            return None
        df = self._std_cn_bars(raw, sym)
        df["adj_factor"] = self._stock_hfq_factor(ak, sym, df["date"])
        return df

    @staticmethod
    def _stock_hfq_factor(ak, sym: str, dates: pd.Series):
        """新浪后复权因子,按日期 asof 对齐(因子只在除权日跳变,只增不改)。

        拉取失败退回 1.0 并告警——宁可当日复权口径缺失待人工补拉,
        也不让整个日更因为一只票的因子接口挂掉。
        """
        try:
            fac = ak.stock_zh_a_daily(symbol=_prefix(sym, "stock"), adjust="hfq-factor")
            if "date" not in fac.columns:
                fac = fac.reset_index()
            col = "hfq_factor" if "hfq_factor" in fac.columns else fac.columns[-1]
            fac = pd.DataFrame({
                "date": pd.to_datetime(fac["date"]),
                "adj_factor": pd.to_numeric(fac[col], errors="coerce"),
            }).dropna().sort_values("date")
            left = pd.DataFrame({"date": pd.to_datetime(dates)})  # 已升序
            out = pd.merge_asof(left, fac, on="date")
            return out["adj_factor"].fillna(1.0).to_numpy()
        except Exception as e:
            logger.warning(f"akshare 复权因子拉取失败 {sym}: {e},本次按 1.0 入库")
            return 1.0

    def _etf_bars(self, ak, sym: str, start, end) -> pd.DataFrame | None:
        kw = dict(symbol=sym, period="daily",
                  start_date=_yyyymmdd(start), end_date=_yyyymmdd(end))
        raw = ak.fund_etf_hist_em(**kw, adjust="")
        if raw is None or len(raw) == 0:
            return None
        df = self._std_cn_bars(raw, sym)
        df["adj_factor"] = 1.0
        try:
            # ETF 无因子接口:后复权收盘 ÷ 不复权收盘 即为因子(分红日跳变)
            hfq = self._std_cn_bars(ak.fund_etf_hist_em(**kw, adjust="hfq"), sym)
            m = df.merge(hfq[["date", "close"]], on="date", suffixes=("", "_hfq"))
            if len(m) == len(df):
                df["adj_factor"] = (pd.to_numeric(m["close_hfq"], errors="coerce")
                                    / pd.to_numeric(m["close"], errors="coerce")
                                    ).fillna(1.0).to_numpy()
        except Exception as e:
            logger.warning(f"akshare ETF 复权因子推算失败 {sym}: {e},按 1.0 入库")
        return df

    @staticmethod
    def _cb_bars(ak, sym: str, start, end) -> pd.DataFrame | None:
        raw = ak.bond_zh_hs_cov_daily(symbol=_prefix(sym, "cb"))   # 全历史
        if raw is None or len(raw) == 0:
            return None
        df = raw.copy()
        if "date" not in df.columns:
            df = df.reset_index()
        miss = [c for c in ("open", "high", "low", "close") if c not in df.columns]
        if miss:
            raise DataSourceError(f"bond_zh_hs_cov_daily 缺列 {miss}(akshare 版本变更?)")
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        if not len(df):
            return None
        keep = [c for c in ("date", "open", "high", "low", "close", "volume")
                if c in df.columns]
        df = df[keep].copy()
        df["symbol"] = sym
        df["adj_factor"] = 1.0        # 转债不复权(§5.2)
        return df.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------- instruments
    def get_instruments(self, sec_type: str) -> pd.DataFrame:
        ak = _ak()
        try:
            if sec_type == "stock":
                raw = ak.stock_info_a_code_name()
                df = pd.DataFrame({"symbol": raw["code"].astype(str),
                                   "name": raw["name"].astype(str)})
            elif sec_type == "etf":
                raw = ak.fund_etf_spot_em()
                df = pd.DataFrame({"symbol": raw["代码"].astype(str),
                                   "name": raw["名称"].astype(str)})
            elif sec_type == "cb":
                raw = ak.bond_zh_cov()
                df = pd.DataFrame({"symbol": raw["债券代码"].astype(str),
                                   "name": raw["债券简称"].astype(str)})
                if "上市时间" in raw.columns:
                    df["list_date"] = pd.to_datetime(raw["上市时间"], errors="coerce")
            else:
                raise DataSourceError(f"akshare 不支持的品种类型: {sec_type}")
        except DataSourceError:
            raise
        except Exception as e:
            raise DataSourceError(f"akshare get_instruments({sec_type}) 失败: {e}") from e
        df["sec_type"] = sec_type
        df["exchange"] = [_prefix(s, sec_type)[:2].upper() for s in df["symbol"]]
        for c in ("list_date", "delist_date"):
            if c not in df.columns:
                df[c] = pd.NaT
        df["status"] = "L"    # 现存列表类接口只返回在市标的
        return df

    # ---------------------------------------------------------- cb_terms
    def get_cb_terms(self) -> pd.DataFrame:
        ak = _ak()
        try:
            raw = ak.bond_zh_cov()
        except Exception as e:
            raise DataSourceError(f"akshare bond_zh_cov 失败: {e}") from e
        ren = {"债券代码": "symbol", "正股代码": "stock_code", "转股价": "conv_price"}
        miss = [c for c in ren if c not in raw.columns]
        if miss:
            raise DataSourceError(f"bond_zh_cov 缺列 {miss}(akshare 版本变更?)")
        df = raw.rename(columns=ren)[list(ren.values())].copy()
        df["symbol"] = df["symbol"].astype(str)
        df["stock_code"] = df["stock_code"].astype(str)
        df["conv_price"] = pd.to_numeric(df["conv_price"], errors="coerce")
        # 赎回状态来自集思录接口;失败则缺列,由备源或人工补(方案 §12.2 已知风险)
        try:
            js = ak.bond_cb_redeem_jsl()
            code_col = "代码" if "代码" in js.columns else "转债代码"
            st_col = "强赎状态" if "强赎状态" in js.columns else "赎回状态"
            m = dict(zip(js[code_col].astype(str), js[st_col].astype(str)))
            df["redeem_status"] = df["symbol"].map(m)
        except Exception as e:
            logger.warning(f"akshare 强赎状态拉取失败: {e}(本次条款不含 redeem_status)")
        return df

    # ---------------------------------------------------------- calendar
    def get_calendar(self, start, end) -> pd.DatetimeIndex:
        ak = _ak()
        try:
            raw = ak.tool_trade_date_hist_sina()
            dates = pd.DatetimeIndex(pd.to_datetime(raw["trade_date"]))
        except Exception as e:
            raise DataSourceError(f"akshare tool_trade_date_hist_sina 失败: {e}") from e
        return dates[(dates >= pd.Timestamp(start))
                     & (dates <= pd.Timestamp(end))].sort_values()
