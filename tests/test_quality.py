"""Task 3 数据质检测试:missing_bar / price_jump / ohlc_invalid / duplicate 四类体检。

夹具:tmp_path 下的 DataStore + 2024 年 1 月工作日日历 + 四只标的
(两只存续 ETF、一只存续转债、一只已退市股票——退市标的不要求有 K 线)。
所有测试不打网络。
"""
import pandas as pd
import pytest

from trend.data.quality import check
from trend.data.store import DataStore

D = pd.Timestamp("2024-01-10")      # 周三,交易日
SAT = pd.Timestamp("2024-01-13")    # 周六,非交易日


@pytest.fixture
def store(tmp_path):
    s = DataStore(tmp_path / "store")
    s.write_calendar(pd.date_range("2024-01-01", "2024-01-31", freq="B"))
    s.upsert_instruments(pd.DataFrame({
        "symbol":      ["510300", "510500", "113001", "600001"],
        "name":        ["沪深300ETF", "中证500ETF", "某转债", "已退市股"],
        "sec_type":    ["etf", "etf", "cb", "stock"],
        "exchange":    ["SH", "SH", "SH", "SH"],
        "list_date":   ["2020-01-01"] * 4,
        "delist_date": [None, None, None, "2023-06-30"],
        "status":      ["L", "L", "L", "D"],
    }))
    return s


def bar(symbol, close, date=D, pre_close=None, **kw):
    """单行合法 K 线:close 落在 [low, high] 内,open 亦然。"""
    row = {"symbol": symbol, "date": date, "open": close * 0.995,
           "high": close * 1.01, "low": close * 0.99, "close": close,
           "volume": 1e6, "amount": close * 1e6, "adj_factor": 1.0}
    if pre_close is not None:
        row["pre_close"] = pre_close
    row.update(kw)
    return pd.DataFrame([row])


def write_clean_day(s):
    """三只存续标的的干净 K 线(涨跌幅均远小于阈值)。"""
    s.write_bars(bar("510300", 10.2, pre_close=10.0), "etf")
    s.write_bars(bar("510500", 5.05, pre_close=5.0), "etf")
    s.write_bars(bar("113001", 110.0, pre_close=108.0), "cb")


def kinds(rep):
    return [(i.symbol, i.kind) for i in rep.issues]


# ---------------------------------------------------------------- 通过 / 缺失
def test_all_clean(store):
    write_clean_day(store)
    rep = check(store, D)
    assert rep.ok and rep.issues == []


def test_missing_bar_flagged_and_suspend_exempts(store):
    store.write_bars(bar("510300", 10.2, pre_close=10.0), "etf")
    store.write_bars(bar("113001", 110.0, pre_close=108.0), "cb")
    rep = check(store, D)
    assert not rep.ok
    assert kinds(rep) == [("510500", "missing_bar")]
    # 有停牌记录 → 不再要求 K 线
    store.write_suspend(pd.DataFrame({"symbol": ["510500"], "date": [D]}))
    assert check(store, D).ok


def test_delisted_symbol_not_required(store):
    write_clean_day(store)
    rep = check(store, D)
    assert all(i.symbol != "600001" for i in rep.issues)


def test_non_trading_day_ok(store):
    """周六无 K 线不算缺失。"""
    assert check(store, SAT).ok


def test_explicit_calendar_param(store):
    from trend.calendar import TradeCalendar
    cal = TradeCalendar(pd.date_range("2024-01-01", "2024-01-31", freq="B"))
    assert check(store, SAT, calendar=cal).ok
    rep = check(store, D, calendar=cal)   # 交易日全缺 K 线 → 三只存续标的 missing_bar
    assert not rep.ok and len(rep.issues) == 3
    assert {i.kind for i in rep.issues} == {"missing_bar"}


# ---------------------------------------------------------------- price_jump
def test_price_jump(store):
    write_clean_day(store)
    store.write_bars(bar("510300", 11.2, pre_close=10.0), "etf")   # +12% > 10%×1.05
    rep = check(store, D)
    assert kinds(rep) == [("510300", "price_jump")]


def test_price_jump_cb_threshold(store):
    """转债涨跌幅限制 20%:+20% 不算异常,+30% 才算。"""
    write_clean_day(store)
    store.write_bars(bar("113001", 129.6, pre_close=108.0), "cb")  # +20% < 20%×1.05
    assert check(store, D).ok
    store.write_bars(bar("113001", 140.4, pre_close=108.0), "cb")  # +30%
    rep = check(store, D)
    assert kinds(rep) == [("113001", "price_jump")]


def test_price_jump_limit_price_exempt(store):
    """收盘正好等于 up_limit → 真实涨停(limit_pct 推断偏差),不报异常。"""
    write_clean_day(store)
    store.write_bars(bar("510300", 11.5, pre_close=10.0, up_limit=11.5), "etf")
    assert check(store, D).ok


def test_price_jump_prev_close_fallback(store):
    """行内无 pre_close 时,回溯最近一根 K 线的收盘价作为前收盘。"""
    write_clean_day(store)
    prev = D - pd.Timedelta(days=1)                    # 2024-01-09 周二
    store.write_bars(bar("510300", 10.0, date=prev, pre_close=9.9), "etf")
    store.write_bars(bar("510300", 11.2), "etf")       # 无 pre_close,+12%
    rep = check(store, D)
    assert kinds(rep) == [("510300", "price_jump")]


# ---------------------------------------------------------------- ohlc_invalid
def test_ohlc_high_below_low(store):
    write_clean_day(store)
    bad = bar("510300", 10.05, pre_close=10.0)
    bad["high"], bad["low"] = 9.0, 11.0
    store.write_bars(bad, "etf")
    rep = check(store, D)
    assert kinds(rep) == [("510300", "ohlc_invalid")]


def test_ohlc_close_outside_range(store):
    write_clean_day(store)
    bad = bar("510500", 5.05, pre_close=5.0)
    bad["close"] = 5.2                                 # > high ≈ 5.10
    store.write_bars(bad, "etf")
    rep = check(store, D)
    assert kinds(rep) == [("510500", "ohlc_invalid")]


# ---------------------------------------------------------------- duplicate
def test_duplicate(store):
    """同一 (symbol,date) 出现两行(如同一标的被写进了 etf 与 stock 两个分区)。"""
    write_clean_day(store)
    store.write_bars(bar("510300", 10.2, pre_close=10.0), "stock")
    rep = check(store, D)
    assert not rep.ok
    assert ("510300", "duplicate") in kinds(rep)


# ---------------------------------------------------------------- 报告
def test_report_summary(store):
    rep = check(store, D)                              # 交易日全缺 K 线
    assert not rep.ok
    assert "missing_bar" in rep.summary() and "510500" in rep.summary()
    write_clean_day(store)
    assert check(store, D).summary() == "质检通过"
