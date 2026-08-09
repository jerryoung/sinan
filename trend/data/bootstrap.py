"""
CSV 种子化入库(方案 §5 / 计划 Task 2):三张全市场 CSV 一次性灌入 DataStore。

源文件(ClickHouse 导出,列结构见各 SQL 的列映射):
    stock_market_data.csv  个股日线  amount 单位**亿元**;(code, date) 有重复行,
                           须按 _version 最大去重(同 _version 的脏重复再按 close/vol
                           取确定性一行——无法判断谁对,只保证可复现)
    etf_market_data.csv    ETF 日线  amount 单位**亿元**;同样去重;无涨跌停列(留 NA)
    bond_market.csv        转债日线  amount 单位**万元**;主键 (bond_code, trade_date)
                           唯一无需去重;价格连续不复权(adj_factor 恒 1)

实现要点(体量:个股 4.4G / 1800 万行):
- DuckDB 直读 CSV,**一次全表扫描**建 TEMP TABLE(去重 + 列映射 + 单位归一到元),
  之后按年 SELECT 经 store.write_bars 落 parquet——大文件只扫一遍,
  单年 DataFrame 体量可控,不会 pandas 爆内存;
- instruments / cb_terms / cb_events / trade_calendar 全部从同一张 TEMP TABLE
  聚合得到,零额外扫描(bootstrap_stock 顺带写 calendar,正是为此);
- 退市近似:标的最后交易日早于全表最大交易日 5 个交易日以上 → status='D',
  delist_date ≈ 最后交易日(长期停牌股会被误判,学习用途可接受,方案已声明);
- 代码列强制 VARCHAR + lpad 补齐 6 位,防前导零丢失(如 002503 / 002022)。

无未来函数问题:本模块只做历史数据搬运,不产生任何信号;cb_events 记录的是
"事件发生日",查询时由 CBTerms.events_until 截断,防未来的责任在读取侧。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from trend.universe.cb_terms import EVT_LAST_TRADE_DAY, EVT_REDEEM_ANNOUNCE

# 判定"已停止交易"的回看窗口:最后交易日早于全表最大交易日 5 个交易日以上视为退市。
# 5 天与转债"强赎公告→摘牌"最短流程量级一致,也吞掉了数据源末尾 1-2 天的更新延迟。
STALE_TRADING_DAYS = 5

_BARS_COLS = ("symbol, date, open, high, low, close, volume, amount, "
              "adj_factor, pre_close, up_limit, down_limit")

# 个股:去重窗口按 _version 最大;同 _version 的脏重复行(实测存在,如 300414
# 2018-07-24 两行内容不同但 _version 相同)再按 close/vol 降序取确定性一行。
_STOCK_SQL = """
CREATE OR REPLACE TEMP TABLE bars_tmp AS
SELECT * EXCLUDE (rn) FROM (
    SELECT
        lpad(stock_code, 6, '0')  AS symbol,
        CAST(trade_date AS DATE)  AS date,
        name,
        open, high, low, close, pre_close,
        vol                       AS volume,
        amount * 1e8              AS amount,       -- 亿元 → 元
        adj_factor, up_limit, down_limit,
        ROW_NUMBER() OVER (
            PARTITION BY stock_code, trade_date
            ORDER BY _version DESC, close DESC, vol DESC
        ) AS rn
    FROM read_csv('{path}', types={{'stock_code': 'VARCHAR'}})
    {where}
) WHERE rn = 1
"""

_ETF_SQL = """
CREATE OR REPLACE TEMP TABLE bars_tmp AS
SELECT * EXCLUDE (rn) FROM (
    SELECT
        lpad(etf_code, 6, '0')    AS symbol,
        CAST(trade_date AS DATE)  AS date,
        name,
        open, high, low, close, pre_close,
        vol                       AS volume,
        amount * 1e8              AS amount,       -- 亿元 → 元
        adj_factor,
        CAST(NULL AS DOUBLE)      AS up_limit,     -- ETF 源无涨跌停列,留 NA
        CAST(NULL AS DOUBLE)      AS down_limit,
        ROW_NUMBER() OVER (
            PARTITION BY etf_code, trade_date
            ORDER BY _version DESC, close DESC, vol DESC
        ) AS rn
    FROM read_csv('{path}', types={{'etf_code': 'VARCHAR'}})
    {where}
) WHERE rn = 1
"""

_BOND_SQL = """
CREATE OR REPLACE TEMP TABLE bars_tmp AS
SELECT
    lpad(bond_code, 6, '0')       AS symbol,
    CAST(trade_date AS DATE)      AS date,
    bond_name                     AS name,
    open_price                    AS open,
    high_price                    AS high,
    low_price                     AS low,
    close_price                   AS close,
    pre_close_price               AS pre_close,
    vol                           AS volume,
    amount * 1e4                  AS amount,       -- 万元 → 元
    1.0                           AS adj_factor,   -- 转债不复权
    CAST(NULL AS DOUBLE)          AS up_limit,
    CAST(NULL AS DOUBLE)          AS down_limit,
    lpad(stock_code, 6, '0')      AS stock_code,   -- 正股代码(前导零补齐)
    cov_price, redeem_status, rating, remain_years, is_alive
FROM read_csv('{path}', types={{'bond_code': 'VARCHAR', 'stock_code': 'VARCHAR',
    'redeem_status': 'VARCHAR', 'rating': 'VARCHAR', 'is_alive': 'VARCHAR'}})
{where}
"""


# ---------------------------------------------------------------- 内部工具
def _connect() -> duckdb.DuckDBPyConnection:
    """内存连接 + 磁盘 spill 目录,大表去重排序不至于 OOM。"""
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{tempfile.mkdtemp(prefix='trend_bootstrap_')}'")
    return con


def _start_ts(start) -> pd.Timestamp | None:
    """start 支持年份(2015 / '2015')或任意日期字符串,年份视为该年 1 月 1 日。"""
    if start is None:
        return None
    s = str(start)
    if s.isdigit() and len(s) == 4:
        return pd.Timestamp(int(s), 1, 1)
    return pd.Timestamp(s)


def _where(start) -> str:
    ts = _start_ts(start)
    return f"WHERE trade_date >= DATE '{ts.date()}'" if ts is not None else ""


def _write_bars_by_year(con, store, sec_type: str) -> int:
    """TEMP TABLE 按年切片经 store.write_bars 落盘,返回总行数。"""
    years = [r[0] for r in
             con.execute("SELECT DISTINCT year(date) FROM bars_tmp ORDER BY 1").fetchall()]
    total = 0
    for y in years:
        df = con.execute(
            f"SELECT {_BARS_COLS} FROM bars_tmp WHERE year(date) = ? ORDER BY symbol, date",
            [y]).df()
        store.write_bars(df, sec_type)
        total += len(df)
        logger.info("{} {} 年: {} 行", sec_type, y, len(df))
    return total


def _exchange(symbol: str, sec_type: str) -> str:
    """由 6 位代码推断交易所:股 6→SH、0/3→SZ、其余(8/4/9 开头)→BJ;
    ETF 5→SH、1→SZ;转债 11→SH、12→SZ。"""
    if sec_type == "stock":
        if symbol.startswith("6"):
            return "SH"
        if symbol.startswith(("0", "3")):
            return "SZ"
        return "BJ"
    if sec_type == "etf":
        return "SH" if symbol.startswith("5") else "SZ"
    return "SH" if symbol.startswith("11") else "SZ"  # cb


def _active_cutoff(dates: pd.DatetimeIndex) -> pd.Timestamp:
    """存续判定线:全表最大交易日往前推 STALE_TRADING_DAYS 个交易日。"""
    dates = pd.DatetimeIndex(dates).sort_values()
    i = max(len(dates) - 1 - STALE_TRADING_DAYS, 0)
    return dates[i]


def _build_instruments(con, sec_type: str, extra_dead: pd.Series | None = None) -> pd.DataFrame:
    """按 symbol 聚合 min/max trade_date → list_date / 近似 delist_date 与存续状态。

    extra_dead:{symbol: bool} 额外退市判定(转债用最新 is_alive=false)。
    """
    inst = con.execute("""
        SELECT symbol,
               arg_max(name, date) AS name,       -- 取最新名称(含 ST 标记等变化)
               min(date)           AS list_date,
               max(date)           AS last_date
        FROM bars_tmp GROUP BY symbol
    """).df()
    all_dates = con.execute("SELECT DISTINCT date FROM bars_tmp").df()["date"]
    cutoff = _active_cutoff(pd.DatetimeIndex(pd.to_datetime(all_dates)))

    inst["list_date"] = pd.to_datetime(inst["list_date"])
    inst["last_date"] = pd.to_datetime(inst["last_date"])
    dead = inst["last_date"] < cutoff
    if extra_dead is not None:
        dead |= inst["symbol"].map(extra_dead).fillna(False).astype(bool)
    inst["sec_type"] = sec_type
    inst["exchange"] = [_exchange(s, sec_type) for s in inst["symbol"]]
    inst["status"] = np.where(dead, "D", "L")
    inst["delist_date"] = inst["last_date"].where(dead)   # 存续标的无退市日
    return inst[["symbol", "name", "sec_type", "exchange",
                 "list_date", "delist_date", "status"]]


def _summary(sec_type: str, rows: int, inst: pd.DataFrame) -> dict:
    return {"sec_type": sec_type, "rows": rows, "symbols": len(inst),
            "alive": int((inst["status"] == "L").sum())}


# 复权因子清洗:源数据存在**孤立 NaN**(实测 510500 2020-09-18,前后皆 0.28),
# 若按 1.0 填充会在后复权序列上炸出 +264%/−72% 的伪收益对,污染信号与相关性。
# 正确语义与老加载器一致:组内(按 symbol、按日期序)前向填充,组内无先行值
# (上市初期)才回退 1.0。必须在全历史的 TEMP TABLE 阶段做——写盘按年切片,
# 年边界上的 NaN 在切片内看不到前值。
_FACTOR_FFILL_SQL = """
CREATE OR REPLACE TEMP TABLE bars_tmp AS
SELECT * REPLACE (
    COALESCE(
        last_value(adj_factor IGNORE NULLS) OVER (
            PARTITION BY symbol ORDER BY date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        1.0) AS adj_factor)
FROM bars_tmp
"""


# ---------------------------------------------------------------- 对外入口
def bootstrap_stock(store, csv_path: str | Path, start=None) -> dict:
    """个股日线 + instruments + trade_calendar,一次全表扫描完成。

    calendar 取个股表 distinct trade_date 全集(个股覆盖最全,是交易日历的
    天然来源);因此种子化流程里跑过 bootstrap_stock 就无需再跑 bootstrap_calendar。
    """
    with _connect() as con:
        logger.info("读取个股 CSV(唯一一次全表扫描): {}", csv_path)
        con.execute(_STOCK_SQL.format(path=csv_path, where=_where(start)))
        con.execute(_FACTOR_FFILL_SQL)
        rows = _write_bars_by_year(con, store, "stock")
        inst = _build_instruments(con, "stock")
        store.upsert_instruments(inst)
        cal = pd.DatetimeIndex(pd.to_datetime(
            con.execute("SELECT DISTINCT date FROM bars_tmp ORDER BY 1").df()["date"]))
        store.write_calendar(cal)
    out = _summary("stock", rows, inst) | {"calendar_days": len(cal)}
    logger.info("个股种子化完成: {}", out)
    return out


def bootstrap_etf(store, csv_path: str | Path, start=None) -> dict:
    """ETF 日线 + instruments(无涨跌停列,up/down_limit 留 NA)。"""
    with _connect() as con:
        logger.info("读取 ETF CSV: {}", csv_path)
        con.execute(_ETF_SQL.format(path=csv_path, where=_where(start)))
        con.execute(_FACTOR_FFILL_SQL)
        rows = _write_bars_by_year(con, store, "etf")
        inst = _build_instruments(con, "etf")
        store.upsert_instruments(inst)
    out = _summary("etf", rows, inst)
    logger.info("ETF 种子化完成: {}", out)
    return out


def bootstrap_cb(store, csv_path: str | Path, start=None) -> dict:
    """转债日线 + instruments + cb_terms + cb_events。

    cb_events 是转债回测正确性的生命线(漏了强赎会"持有"一只已退市的债):
    - EVT_REDEEM_ANNOUNCE: redeem_status 首次出现"强赎"且不含否定词
      ("不强赎/不提前赎回/不行使")的交易日。注意"预计满足强赎条件"也会命中——
      属保守处理(提前离场),回测端宁可少赚不可持死债;
    - EVT_LAST_TRADE_DAY: 已停止交易的债(最后交易日早于全表最大日 5 个交易日
      以上)在其最后交易日记事件。
    """
    with _connect() as con:
        logger.info("读取转债 CSV: {}", csv_path)
        con.execute(_BOND_SQL.format(path=csv_path, where=_where(start)))
        rows = _write_bars_by_year(con, store, "cb")

        # instruments:除交易日近因外,最新 is_alive=false 也视为退市(如违约债)
        alive_flag = con.execute("""
            SELECT symbol, arg_max(is_alive, date) AS is_alive
            FROM bars_tmp GROUP BY symbol
        """).df()
        extra_dead = pd.Series(
            (alive_flag["is_alive"].str.lower() == "false").values,
            index=alive_flag["symbol"])
        inst = _build_instruments(con, "cb", extra_dead=extra_dead)
        store.upsert_instruments(inst)

        # cb_terms:每债取最新一行(严格 ROW_NUMBER,不做跨行拼凑)
        terms = con.execute("""
            SELECT symbol, stock_code, cov_price AS conv_price,
                   redeem_status, rating, date AS last_date, remain_years
            FROM (SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY symbol ORDER BY date DESC) AS rn
                  FROM bars_tmp) WHERE rn = 1
        """).df()
        terms["last_date"] = pd.to_datetime(terms["last_date"])
        # maturity ≈ 最新交易日 + remain_years 年(365.25 天/年的近似换算;
        # 精确到期日需债券条款源,种子化阶段用行情表近似,日更阶段可覆盖修正)
        terms["maturity"] = terms["last_date"] + pd.to_timedelta(
            terms["remain_years"] * 365.25, unit="D")
        store.upsert_cb_terms(terms[["symbol", "stock_code", "conv_price",
                                     "redeem_status", "rating", "maturity"]])

        # cb_events —— 强赎公告
        redeem = con.execute("""
            SELECT symbol, min(date) AS date
            FROM bars_tmp
            WHERE redeem_status LIKE '%强赎%'
              AND redeem_status NOT LIKE '%不强赎%'
              AND redeem_status NOT LIKE '%不提前赎回%'
              AND redeem_status NOT LIKE '%不行使%'
            GROUP BY symbol
        """).df()
        redeem["event"] = EVT_REDEEM_ANNOUNCE

        # cb_events —— 停止交易日(用 instruments 的近因退市判定,复用同一 cutoff)
        gone = inst[inst["delist_date"].notna()
                    & (inst["delist_date"] < _active_cutoff(
                        pd.DatetimeIndex(pd.to_datetime(con.execute(
                            "SELECT DISTINCT date FROM bars_tmp").df()["date"]))))]
        last_day = pd.DataFrame({"symbol": gone["symbol"],
                                 "date": gone["delist_date"],
                                 "event": EVT_LAST_TRADE_DAY})
        events = pd.concat([redeem[["symbol", "date", "event"]], last_day],
                           ignore_index=True)
        store.write_cb_events(events)

    out = _summary("cb", rows, inst) | {
        "redeem_announce": len(redeem), "last_trade_day": len(last_day)}
    logger.info("转债种子化完成: {}", out)
    return out


def bootstrap_calendar(store, csv_path: str | Path, start=None) -> int:
    """独立扫描个股 CSV 抽取 distinct trade_date 写交易日历。

    仅当**不跑** bootstrap_stock 时才需要(bootstrap_stock 已顺带写入,避免
    对 4.4G 文件的第二次扫描);DuckDB 列裁剪只读 trade_date 一列,开销可控。
    """
    with _connect() as con:
        dates = con.execute(
            f"SELECT DISTINCT CAST(trade_date AS DATE) AS date "
            f"FROM read_csv('{csv_path}') {_where(start)} ORDER BY 1").df()["date"]
    cal = pd.DatetimeIndex(pd.to_datetime(dates))
    store.write_calendar(cal)
    logger.info("交易日历写入 {} 天({} → {})", len(cal),
                cal.min().date() if len(cal) else None,
                cal.max().date() if len(cal) else None)
    return len(cal)
