"""
tushare pro 备用数据源适配器(方案 §5.2)。

token 从 ~/.tushare_token 读取(单行,strip;文件不存在时报清晰错误)。
**token 严禁写入日志、异常文本或任何仓库文件**——所有异常信息先经
_redact() 脱敏(万一第三方错误文本回显了 token,替换为 ***)再抛出。

接口映射(tushare pro 文档口径,变更时只改本文件):
    股票日线   pro.daily + pro.adj_factor(按 ts_code+trade_date 对齐)
    ETF 日线   pro.fund_daily(复权因子暂按 1.0,ETF 分红对趋势方向影响小)
    转债日线   pro.cb_daily(转债不复权)
    品种主数据 pro.stock_basic / fund_basic / cb_basic
    转债条款   pro.cb_basic
    交易日历   pro.trade_cal(SSE, is_open=1)

单位换算集中在 _AMOUNT_MULT:tushare 成交额股票/基金为千元、转债为万元,
store 统一为**元**(实测校验见方案 §12.2 数据源细节待定项)。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import DataSource, DataSourceError

_TOKEN_PATH = Path.home() / ".tushare_token"

# 各日线接口的成交额单位 → 元 的换算倍数(依 tushare 文档;口径变更只改这里)
_AMOUNT_MULT = {"stock": 1e3, "etf": 1e3, "cb": 1e4}

# ts_code 逗号串按批拉取,避免超出单次请求限制
_CHUNK = 100


def _ts_code(symbol: str, sec_type: str) -> str:
    """6 位代码 → tushare ts_code(600519 → 600519.SH)。"""
    if sec_type == "stock":
        ex = "SH" if symbol[0] in "69" else ("BJ" if symbol[0] in "48" else "SZ")
    elif sec_type == "etf":
        ex = "SH" if symbol[0] == "5" else "SZ"
    else:  # cb:11x 沪、12x 深
        ex = "SH" if symbol.startswith("11") else "SZ"
    return f"{symbol}.{ex}"


class TushareSource(DataSource):
    name = "tushare"

    _BAR_API = {"stock": "daily", "etf": "fund_daily", "cb": "cb_daily"}

    def __init__(self, token_path: str | Path | None = None) -> None:
        self._token_path = Path(token_path) if token_path is not None else _TOKEN_PATH
        self._token: str | None = None
        self._pro = None

    # ------------------------------------------------------------- 内部
    def _redact(self, text: str) -> str:
        """异常文本脱敏:任何场景下 token 都不允许出现在日志/异常里。"""
        if self._token:
            text = text.replace(self._token, "***")
        return text

    def _api(self):
        if self._pro is not None:
            return self._pro
        p = self._token_path
        if not p.exists():
            raise DataSourceError(
                f"tushare token 文件不存在: {p}(请将 pro token 单行写入该文件)")
        token = p.read_text(encoding="utf-8").strip()
        if not token:
            raise DataSourceError(f"tushare token 文件为空: {p}")
        self._token = token
        try:
            import tushare as ts
        except Exception as e:
            raise DataSourceError(f"tushare 不可用(未安装?): {e}") from e
        try:
            self._pro = ts.pro_api(token)
        except Exception as e:
            raise DataSourceError(
                f"tushare pro_api 初始化失败: {self._redact(str(e))}") from e
        return self._pro

    def _query(self, api_name: str, **kw) -> pd.DataFrame:
        pro = self._api()
        try:
            return getattr(pro, api_name)(**kw)
        except Exception as e:
            raise DataSourceError(
                f"tushare {api_name} 调用失败: {self._redact(str(e))}") from e

    # ------------------------------------------------------------- bars
    def get_bars(self, symbols: list[str], sec_type: str, start, end) -> pd.DataFrame:
        if sec_type not in self._BAR_API:
            raise DataSourceError(f"tushare 不支持的品种类型: {sec_type}")
        s = pd.Timestamp(start).strftime("%Y%m%d")
        e = pd.Timestamp(end).strftime("%Y%m%d")
        code_map = {_ts_code(str(x), sec_type): str(x) for x in symbols}
        codes = list(code_map)
        frames = []
        for i in range(0, len(codes), _CHUNK):
            raw = self._query(self._BAR_API[sec_type],
                              ts_code=",".join(codes[i:i + _CHUNK]),
                              start_date=s, end_date=e)
            if raw is not None and len(raw):
                frames.append(raw)
        if not frames:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close"])
        df = pd.concat(frames, ignore_index=True)
        need = ["ts_code", "trade_date", "open", "high", "low", "close"]
        miss = [c for c in need if c not in df.columns]
        if miss:
            raise DataSourceError(
                f"tushare {self._BAR_API[sec_type]} 返回缺列 {miss}(接口变更?)")
        out = pd.DataFrame({
            "symbol": df["ts_code"].map(code_map),
            "date": pd.to_datetime(df["trade_date"]),
            "open": df["open"], "high": df["high"],
            "low": df["low"], "close": df["close"],
        })
        if "vol" in df.columns:
            out["volume"] = df["vol"]
        if "amount" in df.columns:
            out["amount"] = (pd.to_numeric(df["amount"], errors="coerce")
                             * _AMOUNT_MULT[sec_type])
        if "pre_close" in df.columns:
            out["pre_close"] = df["pre_close"]
        out["adj_factor"] = 1.0
        if sec_type == "stock":
            out = self._merge_adj_factor(out, codes, code_map, s, e)
        return (out.dropna(subset=["symbol"])
                   .sort_values(["symbol", "date"]).reset_index(drop=True))

    def _merge_adj_factor(self, out: pd.DataFrame, codes: list[str],
                          code_map: dict[str, str], s: str, e: str) -> pd.DataFrame:
        """股票复权因子按 (ts_code, trade_date) 精确对齐;拉不到的行保持 1.0。"""
        frames = []
        for i in range(0, len(codes), _CHUNK):
            try:
                fac = self._query("adj_factor", ts_code=",".join(codes[i:i + _CHUNK]),
                                  start_date=s, end_date=e)
                if fac is not None and len(fac):
                    frames.append(fac)
            except DataSourceError:
                continue  # 因子缺失不整体失败,待人工补拉
        if not frames:
            return out
        fac = pd.concat(frames, ignore_index=True)
        if not {"ts_code", "trade_date", "adj_factor"} <= set(fac.columns):
            return out
        fac = pd.DataFrame({
            "symbol": fac["ts_code"].map(code_map),
            "date": pd.to_datetime(fac["trade_date"]),
            "_af": pd.to_numeric(fac["adj_factor"], errors="coerce"),
        })
        out = out.merge(fac, on=["symbol", "date"], how="left")
        out["adj_factor"] = out["_af"].fillna(1.0)
        return out.drop(columns=["_af"])

    # ------------------------------------------------------- instruments
    def get_instruments(self, sec_type: str) -> pd.DataFrame:
        if sec_type == "stock":
            frames = []
            for status in ("L", "D", "P"):    # 在市/退市/暂停上市
                try:
                    raw = self._query("stock_basic", exchange="", list_status=status,
                                      fields="ts_code,name,list_date,delist_date")
                    if raw is not None and len(raw):
                        raw = raw.copy()
                        raw["status"] = status
                        frames.append(raw)
                except DataSourceError:
                    if status == "L":
                        raise             # 主列表都拉不到才算失败
            raw = pd.concat(frames, ignore_index=True)
        elif sec_type == "etf":
            raw = self._query("fund_basic", market="E")
        elif sec_type == "cb":
            raw = self._query("cb_basic")
        else:
            raise DataSourceError(f"tushare 不支持的品种类型: {sec_type}")

        if raw is None or len(raw) == 0 or "ts_code" not in raw.columns:
            return pd.DataFrame()
        name_col = "bond_short_name" if sec_type == "cb" else "name"
        df = pd.DataFrame({
            "symbol": raw["ts_code"].astype(str).str[:6],
            "name": raw[name_col] if name_col in raw.columns else "",
            "list_date": pd.to_datetime(
                raw["list_date"], errors="coerce") if "list_date" in raw.columns else pd.NaT,
            "delist_date": pd.to_datetime(
                raw["delist_date"], errors="coerce") if "delist_date" in raw.columns else pd.NaT,
            "status": raw["status"] if "status" in raw.columns else "L",
            "exchange": raw["ts_code"].astype(str).str[-2:],
        })
        df["sec_type"] = sec_type
        return df

    # ---------------------------------------------------------- cb_terms
    def get_cb_terms(self) -> pd.DataFrame:
        raw = self._query("cb_basic")
        if raw is None or len(raw) == 0 or "ts_code" not in raw.columns:
            return pd.DataFrame()
        df = pd.DataFrame({"symbol": raw["ts_code"].astype(str).str[:6]})
        for src_col, dst in (("stk_code", "stock_code"), ("conv_price", "conv_price"),
                             ("rating", "rating"), ("maturity_date", "maturity")):
            if src_col in raw.columns:
                df[dst] = raw[src_col].values
        if "stock_code" in df.columns:
            df["stock_code"] = df["stock_code"].astype(str).str[:6]
        if "conv_price" in df.columns:
            df["conv_price"] = pd.to_numeric(df["conv_price"], errors="coerce")
        if "maturity" in df.columns:
            df["maturity"] = pd.to_datetime(df["maturity"], errors="coerce")
        return df

    # ---------------------------------------------------------- calendar
    def get_calendar(self, start, end) -> pd.DatetimeIndex:
        raw = self._query("trade_cal", exchange="SSE",
                          start_date=pd.Timestamp(start).strftime("%Y%m%d"),
                          end_date=pd.Timestamp(end).strftime("%Y%m%d"),
                          is_open="1")
        if raw is None or len(raw) == 0 or "cal_date" not in raw.columns:
            raise DataSourceError("tushare trade_cal 返回为空或缺 cal_date 列")
        return pd.DatetimeIndex(pd.to_datetime(raw["cal_date"])).sort_values()
