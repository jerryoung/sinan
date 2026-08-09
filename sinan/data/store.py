"""
存储层(方案 §5):parquet(按年分区)+ DuckDB 查询,零运维、单目录备份。

所有读写收敛到 DataStore 薄接口,上层代码不直接接触 SQL 与文件路径;
后期如需服务型数据库,仅需新增一个 DataStore 实现,上层零改动。

表设计(§5.1):
    bars_daily     (symbol, date) 主键,存**不复权价** + adj_factor(只增不改,
                   后复权 = 原始 OHLC × adj_factor;转债恒为 1)
                   可选列 pre_close / up_limit / down_limit 供涨跌停判定
    instruments    symbol 主键:name, sec_type, exchange, list_date, delist_date, status
    cb_terms       symbol 主键:conv_price(转股价), stock_code, redeem_status, rating, maturity
    cb_events      (symbol, date, event):强赎公告/停止交易日/摘牌 事件流
    trade_calendar date 主键
    suspend        (symbol, date) 停牌记录

单位约定:amount 一律为**元**(写入前由调用方归一);volume 保持数据源原始单位,
流动性检查一律用 amount。
(§12.3:如未来做分钟级,仅在此新增 bars_min 分区表,schema 同 bars_daily + time 列。)
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

BARS_REQUIRED = ["symbol", "date", "open", "high", "low", "close"]
BARS_OPTIONAL = ["volume", "amount", "adj_factor", "pre_close", "up_limit", "down_limit"]
BARS_COLS = BARS_REQUIRED + BARS_OPTIONAL

_TABLES = {  # 小表:文件名 -> 去重主键
    "instruments": ["symbol"],
    "cb_terms": ["symbol"],
    "cb_events": ["symbol", "date", "event"],
    "trade_calendar": ["date"],
    "suspend": ["symbol", "date"],
}


class DataStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "bars_daily").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # bars_daily
    # ------------------------------------------------------------------
    def write_bars(self, df: pd.DataFrame, sec_type: str) -> None:
        """upsert:按 (symbol, date) 去重保留最新,按年分区重写受影响分区。"""
        if df is None or len(df) == 0:
            return
        df = df.copy()
        missing = [c for c in BARS_REQUIRED if c not in df.columns]
        if missing:
            raise ValueError(f"bars 缺少必需列: {missing}")
        df["symbol"] = df["symbol"].astype(str)
        df["date"] = pd.to_datetime(df["date"])
        for c in BARS_OPTIONAL:
            if c not in df.columns:
                df[c] = pd.NA
        # NaN 因子:组内前向填充(与源数据"因子只增不改、缺失即沿用"的语义一致),
        # 仅组内无先行值时回退 1.0。注意:本方法只能看到本次调用的帧——跨年/跨批
        # 的 NaN 由 bootstrap 的全历史 TEMP TABLE 阶段兜底(_FACTOR_FFILL_SQL)。
        df = df.sort_values(["symbol", "date"])
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        df["adj_factor"] = df.groupby("symbol")["adj_factor"].ffill().fillna(1.0)
        for c in ["open", "high", "low", "close", "volume", "amount",
                  "pre_close", "up_limit", "down_limit"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[BARS_COLS]

        for year, part in df.groupby(df["date"].dt.year):
            pdir = self.root / "bars_daily" / f"sec_type={sec_type}" / f"year={year}"
            pdir.mkdir(parents=True, exist_ok=True)
            fp = pdir / "data.parquet"
            if fp.exists():
                old = pd.read_parquet(fp)
                part = pd.concat([old, part], ignore_index=True)
            part = (part.drop_duplicates(["symbol", "date"], keep="last")
                        .sort_values(["symbol", "date"]))
            part.to_parquet(fp, index=False)

    def read_bars(
        self,
        symbols: list[str] | None = None,
        sec_type: str | None = None,
        start=None,
        end=None,
        adjust: bool = False,
    ) -> pd.DataFrame:
        """读日线;adjust=True 返回后复权 OHLC,并保留 close_raw 原始收盘价。"""
        glob = str(self.root / "bars_daily" / "**" / "*.parquet")
        if not any((self.root / "bars_daily").rglob("*.parquet")):
            cols = BARS_COLS + ["sec_type"] + (["close_raw"] if adjust else [])
            return pd.DataFrame(columns=cols)

        conds, params = [], []
        if sec_type is not None:
            conds.append("sec_type = ?"); params.append(sec_type)
        if symbols:
            conds.append(f"symbol IN ({','.join('?' * len(symbols))})")
            params.extend(str(s) for s in symbols)
        if start is not None:
            conds.append("date >= ?"); params.append(pd.Timestamp(start).to_pydatetime())
        if end is not None:
            conds.append("date <= ?"); params.append(pd.Timestamp(end).to_pydatetime())
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = (f"SELECT * FROM read_parquet('{glob}', hive_partitioning=1, "
               f"union_by_name=1) {where} ORDER BY symbol, date")
        with duckdb.connect() as con:
            out = con.execute(sql, params).df()
        out["date"] = pd.to_datetime(out["date"])
        if "year" in out.columns:
            out = out.drop(columns=["year"])
        if adjust:
            f = out["adj_factor"].fillna(1.0)
            out["close_raw"] = out["close"]
            for c in ["open", "high", "low", "close"]:
                out[c] = out[c] * f
        return out.reset_index(drop=True)

    def coverage(self) -> pd.DataFrame:
        """数据仓覆盖概况:各品种的标的数/行数/起止日期(duckdb 聚合,不加载明细)。"""
        cols = ["sec_type", "n_symbols", "n_rows", "min_date", "max_date"]
        glob = str(self.root / "bars_daily" / "**" / "*.parquet")
        if not any((self.root / "bars_daily").rglob("*.parquet")):
            return pd.DataFrame(columns=cols)
        sql = (f"SELECT sec_type, COUNT(DISTINCT symbol) AS n_symbols, "
               f"COUNT(*) AS n_rows, MIN(date) AS min_date, MAX(date) AS max_date "
               f"FROM read_parquet('{glob}', hive_partitioning=1, union_by_name=1) "
               f"GROUP BY sec_type ORDER BY sec_type")
        with duckdb.connect() as con:
            out = con.execute(sql).df()
        for c in ("min_date", "max_date"):
            out[c] = pd.to_datetime(out[c])
        return out[cols]

    # ------------------------------------------------------------------
    # 小表通用 upsert / read
    # ------------------------------------------------------------------
    def _upsert(self, table: str, df: pd.DataFrame) -> None:
        if df is None or len(df) == 0:
            return
        keys = _TABLES[table]
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str)
        fp = self.root / f"{table}.parquet"
        if fp.exists():
            old = pd.read_parquet(fp)
            df = pd.concat([old, df], ignore_index=True)
        df = df.drop_duplicates(keys, keep="last").sort_values(keys)
        df.to_parquet(fp, index=False)

    def _read(self, table: str) -> pd.DataFrame:
        fp = self.root / f"{table}.parquet"
        if not fp.exists():
            return pd.DataFrame()
        return pd.read_parquet(fp)

    # instruments ------------------------------------------------------
    def upsert_instruments(self, df: pd.DataFrame) -> None:
        self._upsert("instruments", df)

    def read_instruments(self, sec_type: str | None = None) -> pd.DataFrame:
        df = self._read("instruments")
        if len(df) and sec_type is not None:
            df = df[df["sec_type"] == sec_type].reset_index(drop=True)
        return df

    # cb ---------------------------------------------------------------
    def upsert_cb_terms(self, df: pd.DataFrame) -> None:
        self._upsert("cb_terms", df)

    def read_cb_terms(self, symbols: list[str] | None = None) -> pd.DataFrame:
        df = self._read("cb_terms")
        if len(df) and symbols is not None:
            df = df[df["symbol"].isin([str(s) for s in symbols])].reset_index(drop=True)
        return df

    def write_cb_events(self, df: pd.DataFrame) -> None:
        self._upsert("cb_events", df)

    def read_cb_events(self, symbols: list[str] | None = None) -> pd.DataFrame:
        df = self._read("cb_events")
        if len(df) and symbols is not None:
            df = df[df["symbol"].isin([str(s) for s in symbols])].reset_index(drop=True)
        return df

    # calendar ---------------------------------------------------------
    def write_calendar(self, dates) -> None:
        self._upsert("trade_calendar", pd.DataFrame({"date": pd.DatetimeIndex(dates)}))

    def read_calendar(self, start=None, end=None) -> pd.DatetimeIndex:
        df = self._read("trade_calendar")
        if len(df) == 0:
            return pd.DatetimeIndex([])
        idx = pd.DatetimeIndex(pd.to_datetime(df["date"])).sort_values()
        if start is not None:
            idx = idx[idx >= pd.Timestamp(start)]
        if end is not None:
            idx = idx[idx <= pd.Timestamp(end)]
        return idx

    # suspend ----------------------------------------------------------
    def write_suspend(self, df: pd.DataFrame) -> None:
        self._upsert("suspend", df)

    def read_suspend(self, symbols: list[str] | None = None) -> pd.DataFrame:
        df = self._read("suspend")
        if len(df) and symbols is not None:
            df = df[df["symbol"].isin([str(s) for s in symbols])].reset_index(drop=True)
        return df
