"""Task 2 种子化测试:去重 / 单位归一 / instruments / cb_terms / cb_events / calendar。

夹具为真实全市场 CSV 的裁剪(DuckDB COPY 生成,列结构与大文件完全一致),覆盖:
    stock_sample.csv  600519 贵州茅台(存续)、002503 搜于特(2023-05-22 停止交易→退市)、
                      300414 中光防雷(2018-07-24 存在同 (code,date) 重复行,验证去重)
    etf_sample.csv    510300 / 159915(2026-07-20 各有一条 ClickHouse 导出重复行)
    bond_sample.csv   123078 飞凯转债(先"公告不强赎"后 2025-04-28"公告强赎",
                      2025-05-16 停止交易 → redeem_announce + last_trade_day 双事件)、
                      128124 科华转债("公告到期赎回",不含"强赎"字样,不得触发事件)、
                      110097 天润转债(存续,无事件)

测试策略:用 pandas 独立实现一遍去重/单位换算作为参考值,与 DuckDB 管道产出对账,
避免"实现抄一遍当预期"的自证。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trend.data.bootstrap import (
    bootstrap_calendar,
    bootstrap_cb,
    bootstrap_etf,
    bootstrap_stock,
)
from trend.data.store import DataStore
from trend.universe.cb_terms import EVT_LAST_TRADE_DAY, EVT_REDEEM_ANNOUNCE

FIX = Path(__file__).parent / "fixtures"
STOCK_CSV = FIX / "stock_sample.csv"
ETF_CSV = FIX / "etf_sample.csv"
BOND_CSV = FIX / "bond_sample.csv"


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "store")


def _dedup(raw: pd.DataFrame, code_col: str) -> pd.DataFrame:
    """pandas 参考实现:按 (code, trade_date) 保留 _version 最大的一行。

    同 _version 的脏重复行(如 300414 2018-07-24)再按 close/vol 定序取确定性一行,
    与 bootstrap 的 SQL ROW_NUMBER 排序规则一致。
    """
    return (raw.sort_values([code_col, "trade_date", "_version", "close", "vol"])
               .drop_duplicates([code_col, "trade_date"], keep="last"))


# ---------------------------------------------------------------- etf
def test_bootstrap_etf(store):
    raw = pd.read_csv(ETF_CSV, dtype={"etf_code": str}, parse_dates=["trade_date"])
    assert raw.duplicated(["etf_code", "trade_date"]).any()  # 夹具守卫:必须含重复行
    info = bootstrap_etf(store, ETF_CSV)

    bars = store.read_bars(sec_type="etf")
    ref = _dedup(raw, "etf_code")
    assert len(bars) == len(ref) == info["rows"]
    assert not bars.duplicated(["symbol", "date"]).any()

    # 单位归一:亿元 → 元;OHLC/pre_close 原样;ETF 无涨跌停列 → NA
    r0 = ref[ref["etf_code"] == "510300"].iloc[-1]
    got = bars[(bars["symbol"] == "510300") & (bars["date"] == r0["trade_date"])].iloc[0]
    assert np.isclose(got["amount"], r0["amount"] * 1e8)
    assert np.isclose(got["close"], r0["close"])
    assert np.isclose(got["pre_close"], r0["pre_close"])
    assert np.isclose(got["adj_factor"], r0["adj_factor"])
    assert pd.isna(got["up_limit"]) and pd.isna(got["down_limit"])

    inst = store.read_instruments(sec_type="etf").set_index("symbol")
    assert inst.loc["510300", "exchange"] == "SH"
    assert inst.loc["159915", "exchange"] == "SZ"
    assert (inst["status"] == "L").all()  # 两只都交易到夹具最新日


# ---------------------------------------------------------------- stock
def test_bootstrap_stock(store):
    raw = pd.read_csv(STOCK_CSV, dtype={"stock_code": str}, parse_dates=["trade_date"])
    assert raw.duplicated(["stock_code", "trade_date"]).any()  # 夹具守卫
    bootstrap_stock(store, STOCK_CSV)

    bars = store.read_bars(sec_type="stock")
    ref = _dedup(raw, "stock_code")
    assert len(bars) == len(ref)
    assert not bars.duplicated(["symbol", "date"]).any()

    # 去重:300414 2018-07-24 两行同 _version,须取确定性一行且与参考实现一致
    dup = bars[(bars["symbol"] == "300414") & (bars["date"] == "2018-07-24")]
    ref_dup = ref[(ref["stock_code"] == "300414")
                  & (ref["trade_date"] == "2018-07-24")].iloc[0]
    assert len(dup) == 1
    assert np.isclose(dup.iloc[0]["close"], ref_dup["close"])

    # 单位归一 + adj_factor / 涨跌停列原样保留
    r0 = ref[ref["stock_code"] == "600519"].iloc[-1]
    got = bars[(bars["symbol"] == "600519") & (bars["date"] == r0["trade_date"])].iloc[0]
    assert np.isclose(got["amount"], r0["amount"] * 1e8)
    assert np.isclose(got["adj_factor"], r0["adj_factor"])
    assert np.isclose(got["up_limit"], r0["up_limit"])
    assert np.isclose(got["down_limit"], r0["down_limit"])

    # 复权读取通路:后复权 = 原始价 × adj_factor
    adj = store.read_bars(symbols=["600519"], adjust=True)
    assert np.allclose(adj["close"], adj["close_raw"] * adj["adj_factor"])

    # calendar:个股表 distinct trade_date 全集
    cal = store.read_calendar()
    assert set(cal) == set(pd.to_datetime(raw["trade_date"].unique()))

    # instruments:存续/退市近似判定 + 交易所推断 + 最新名称
    inst = store.read_instruments(sec_type="stock").set_index("symbol")
    assert inst.loc["600519", "status"] == "L"
    assert pd.isna(inst.loc["600519", "delist_date"])
    assert inst.loc["002503", "status"] == "D"
    assert pd.Timestamp(inst.loc["002503", "delist_date"]) == pd.Timestamp("2023-05-22")
    assert inst.loc["600519", "exchange"] == "SH"
    assert inst.loc["002503", "exchange"] == "SZ"
    assert inst.loc["300414", "exchange"] == "SZ"
    assert inst.loc["600519", "name"] == "贵州茅台"
    assert pd.Timestamp(inst.loc["600519", "list_date"]) == raw[
        raw["stock_code"] == "600519"]["trade_date"].min()


def test_bootstrap_stock_start_filter(store):
    """--start 过滤:2023 起 → 2018 年的 300414 整只被排除,calendar 同步收窄。"""
    bootstrap_stock(store, STOCK_CSV, start=2023)
    bars = store.read_bars(sec_type="stock")
    assert (bars["date"] >= "2023-01-01").all()
    assert "300414" not in set(bars["symbol"])
    assert store.read_calendar().min() >= pd.Timestamp("2023-01-01")


# ---------------------------------------------------------------- cb
def test_bootstrap_cb(store):
    raw = pd.read_csv(BOND_CSV, dtype={"bond_code": str, "stock_code": str},
                      parse_dates=["trade_date"])
    # 夹具守卫:123078 先"公告不强赎"后"公告强赎",首个强赎日 2025-04-28
    st = raw["redeem_status"].fillna("")
    hit = raw[st.str.contains("强赎") & ~st.str.contains("不强赎")]
    assert hit["trade_date"].min() == pd.Timestamp("2025-04-28")
    assert (raw.loc[raw["bond_code"] == "123078", "redeem_status"] == "公告不强赎").any()

    bootstrap_cb(store, BOND_CSV)

    # bars:主键唯一无去重损耗;万元 → 元;列名映射;不复权
    bars = store.read_bars(sec_type="cb")
    assert len(bars) == len(raw)
    r0 = raw[raw["bond_code"] == "123078"].iloc[-1]
    got = bars[(bars["symbol"] == "123078") & (bars["date"] == r0["trade_date"])].iloc[0]
    assert np.isclose(got["amount"], r0["amount"] * 1e4)
    assert np.isclose(got["close"], r0["close_price"])
    assert np.isclose(got["open"], r0["open_price"])
    assert np.isclose(got["high"], r0["high_price"])
    assert np.isclose(got["low"], r0["low_price"])
    assert np.isclose(got["pre_close"], r0["pre_close_price"])
    assert (bars["adj_factor"] == 1.0).all()

    # cb_terms:每债最新一行;maturity ≈ 最新交易日 + remain_years
    terms = store.read_cb_terms(["123078"]).iloc[0]
    assert np.isclose(terms["conv_price"], r0["cov_price"])
    assert terms["stock_code"] == r0["stock_code"]
    assert terms["rating"] == r0["rating"]
    expect_mat = r0["trade_date"] + pd.to_timedelta(r0["remain_years"] * 365.25, unit="D")
    assert abs(pd.Timestamp(terms["maturity"]) - expect_mat) <= pd.Timedelta(days=2)
    # 正股代码前导零不丢失(128124 科华转债 → 002022 科华生物)
    assert store.read_cb_terms(["128124"]).iloc[0]["stock_code"] == "002022"

    # cb_events:转债回测正确性的生命线
    ev = store.read_cb_events()
    e78 = ev[ev["symbol"] == "123078"].set_index("event")
    assert pd.Timestamp(e78.loc[EVT_REDEEM_ANNOUNCE, "date"]) == pd.Timestamp("2025-04-28")
    assert pd.Timestamp(e78.loc[EVT_LAST_TRADE_DAY, "date"]) == pd.Timestamp("2025-05-16")
    assert not (ev["symbol"] == "128124").any()  # "公告到期赎回"不含"强赎",不触发
    assert not (ev["symbol"] == "110097").any()  # 存续且无强赎,无事件

    # instruments
    inst = store.read_instruments(sec_type="cb").set_index("symbol")
    assert inst.loc["123078", "status"] == "D"
    assert pd.Timestamp(inst.loc["123078", "delist_date"]) == pd.Timestamp("2025-05-16")
    assert inst.loc["110097", "status"] == "L"
    assert inst.loc["128124", "status"] == "L"
    assert inst.loc["110097", "exchange"] == "SH"   # 11 开头 → 沪
    assert inst.loc["123078", "exchange"] == "SZ"   # 12 开头 → 深


# ---------------------------------------------------------------- calendar / CLI
def test_bootstrap_calendar_standalone(store):
    n = bootstrap_calendar(store, STOCK_CSV)
    raw = pd.read_csv(STOCK_CSV, parse_dates=["trade_date"])
    uniq = pd.to_datetime(raw["trade_date"].unique())
    assert n == len(uniq)
    assert set(store.read_calendar()) == set(uniq)


def test_cli_smoke(tmp_path):
    """CLI 端到端:夹具伪装成 data-dir,--types etf,cb 入库到独立 store。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copy(ETF_CSV, data_dir / "etf_market_data.csv")
    shutil.copy(BOND_CSV, data_dir / "bond_market.csv")
    store_root = tmp_path / "store"
    script = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap_from_csv.py"

    res = subprocess.run(
        [sys.executable, str(script), "--types", "etf,cb",
         "--data-dir", str(data_dir), "--store-root", str(store_root)],
        capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    store = DataStore(store_root)
    assert len(store.read_bars(sec_type="etf")) > 0
    assert len(store.read_bars(sec_type="cb")) > 0
    assert len(store.read_cb_terms()) == 3


# ---------------------------------------------------------------- 复权因子 NaN 清洗
def test_adj_factor_nan_ffill(store, tmp_path):
    """孤立 NaN 因子须组内前向填充(复现 510500 2020-09-18 源数据缺陷)。

    若错误地按 1.0 填充,后复权序列会在 NaN 日炸出 +264%/−72% 级伪收益对,
    污染信号、ATR 与相关性——本用例锁死正确语义:NaN 日沿用前值 0.28,
    上市首日无先行值才回退 1.0。
    """
    hdr = ETF_CSV.read_text(encoding="utf-8").splitlines()[0]
    cols = hdr.split(",")
    days = ["2020-09-16", "2020-09-17", "2020-09-18", "2020-09-21", "2020-09-22"]
    closes = [6.994, 7.009, 7.136, 7.085, 6.997]
    factors = ["0.28", "0.28", "", "0.28", "0.28"]        # 中间一天 NaN
    rows = [hdr]
    for d, c, f in zip(days, closes, factors):
        r = {k: "" for k in cols}
        r.update({"ts_code": "510500.SH", "etf_code": "510500", "trade_date": d,
                  "name": "中证500ETF", "open": c, "high": c, "low": c, "close": c,
                  "pre_close": c, "vol": "1000", "amount": "1.0",
                  "adj_factor": f, "_version": "1"})
        rows.append(",".join(str(r[k]) for k in cols))
    csv = tmp_path / "etf_nan_factor.csv"
    csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    bootstrap_etf(store, csv)
    out = store.read_bars(symbols=["510500"], sec_type="etf", adjust=True)
    out = out.set_index("date").sort_index()
    assert out.loc["2020-09-18", "adj_factor"] == pytest.approx(0.28)   # ffill 而非 1.0
    r = out["close"].pct_change().dropna()
    assert r.abs().max() < 0.05                            # 复权收益无伪跳变
