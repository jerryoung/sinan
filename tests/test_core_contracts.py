"""Task 1 核心契约测试:store roundtrip / 规则推断 / 日历 / ctx 无未来函数。"""
import numpy as np
import pandas as pd
import pytest

from sinan.calendar import TradeCalendar
from sinan.config import load_rules, load_settings, load_strategy
from sinan.data.store import DataStore
from sinan.signal.base import SignalContext, get_strategy, register
from sinan.universe.cb_terms import EVT_REDEEM_ANNOUNCE, CBEvent, CBTerms, build_cb_terms
from sinan.universe.instruments import resolve_rule


def _bars(symbol, dates, base=10.0):
    n = len(dates)
    close = base + np.arange(n) * 0.1
    return pd.DataFrame({
        "symbol": symbol, "date": dates,
        "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
        "close": close, "volume": 1e6, "amount": close * 1e6,
        "adj_factor": 1.0,
    })


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "store")


# ---------------------------------------------------------------- store
def test_store_roundtrip_and_upsert(store):
    dates = pd.date_range("2023-12-25", periods=10, freq="B")  # 跨 2023/2024 两个年分区
    df = _bars("510300", dates)
    store.write_bars(df, "etf")
    out = store.read_bars(symbols=["510300"], sec_type="etf")
    assert len(out) == 10
    assert out["date"].is_monotonic_increasing

    # upsert:同 (symbol,date) 重写应覆盖而非重复
    df2 = df.copy()
    df2["close"] = 99.0
    store.write_bars(df2, "etf")
    out2 = store.read_bars(symbols=["510300"], sec_type="etf")
    assert len(out2) == 10
    assert (out2["close"] == 99.0).all()

    # 区间过滤
    out3 = store.read_bars(sec_type="etf", start="2024-01-01")
    assert (out3["date"] >= "2024-01-01").all() and len(out3) > 0


def test_store_adjust(store):
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    df = _bars("600519", dates)
    df["adj_factor"] = 2.0
    store.write_bars(df, "stock")
    out = store.read_bars(symbols=["600519"], adjust=True)
    assert np.allclose(out["close"], out["close_raw"] * 2.0)
    assert np.allclose(out["open"], (out["close_raw"] - 0.05) * 2.0)


def test_store_lists_symbols_from_actual_bar_partitions(store):
    """行情目录以实际 bars 为准,不能依赖可能缺失或滞后的 instruments。"""
    store.write_bars(_bars("000001", pd.date_range("2024-01-01", periods=3)),
                     "stock")
    store.write_bars(_bars("510300", pd.date_range("2024-01-01", periods=2)),
                     "etf")

    got = store.list_bar_symbols().set_index("symbol")

    assert got.loc["000001", "sec_type"] == "stock"
    assert got.loc["000001", "n_rows"] == 3
    assert got.loc["510300", "sec_type"] == "etf"


def test_store_small_tables(store):
    store.upsert_instruments(pd.DataFrame({
        "symbol": ["510300", "600519"], "name": ["沪深300ETF", "贵州茅台"],
        "sec_type": ["etf", "stock"], "exchange": ["SH", "SH"],
        "list_date": ["2012-05-28", "2001-08-27"], "delist_date": [None, None],
        "status": ["L", "L"]}))
    assert len(store.read_instruments(sec_type="etf")) == 1

    store.write_cb_events(pd.DataFrame({
        "symbol": ["128100"], "date": ["2023-05-20"], "event": [EVT_REDEEM_ANNOUNCE]}))
    store.write_cb_events(pd.DataFrame({   # 同键重写不重复
        "symbol": ["128100"], "date": ["2023-05-20"], "event": [EVT_REDEEM_ANNOUNCE]}))
    assert len(store.read_cb_events(["128100"])) == 1

    store.write_calendar(pd.date_range("2024-01-01", periods=5, freq="B"))
    assert len(store.read_calendar()) == 5


# ---------------------------------------------------------------- rules
@pytest.mark.parametrize("symbol,sec_type,name,limit,t_plus,lot", [
    ("600519", "stock", "贵州茅台", 0.10, 1, 100),
    ("688981", "stock", "中芯国际", 0.20, 1, 100),
    ("300750", "stock", "宁德时代", 0.20, 1, 100),
    ("600666", "stock", "ST瑞德", 0.05, 1, 100),
    ("300xxx", "stock", "ST某创", 0.20, 1, 100),   # 双创 ST 仍 20%
    ("832000", "stock", "北交所样例", 0.30, 1, 100),
    ("510300", "etf", "沪深300ETF", 0.10, 1, 100),
    ("588000", "etf", "科创50ETF", 0.20, 1, 100),
    ("513100", "etf", "纳指ETF", 0.10, 0, 100),    # 跨境 T+0
    ("511010", "etf", "国债ETF", 0.10, 0, 100),
    ("128100", "cb", "搜特转债", 0.20, 0, 10),
])
def test_resolve_rule(symbol, sec_type, name, limit, t_plus, lot):
    r = resolve_rule(symbol, sec_type, load_rules(), name=name)
    assert (r.limit_pct, r.t_plus, r.lot_size) == (limit, t_plus, lot)


def test_stock_costs_from_yaml():
    r = resolve_rule("600519", "stock", load_rules(), name="贵州茅台")
    assert r.stamp_tax_sell > 0 and r.commission > 0 and r.slippage > 0
    r2 = resolve_rule("510300", "etf", load_rules())
    assert r2.stamp_tax_sell == 0.0


# ---------------------------------------------------------------- calendar
def test_calendar():
    days = pd.date_range("2024-01-01", periods=10, freq="B")
    cal = TradeCalendar(days)
    assert cal.is_open(days[3]) and not cal.is_open("2024-01-06")  # 周六
    assert cal.next_open(days[3]) == days[4]
    assert cal.next_open(days[-1]) is None
    assert cal.prev_open(days[3]) == days[2]
    assert len(cal.sessions(days[2], days[5])) == 4


# ---------------------------------------------------------------- context
def test_ctx_no_lookahead():
    dates = pd.date_range("2024-01-01", periods=20, freq="B")
    df = _bars("510300", dates).set_index("date")
    ctx = SignalContext(
        today=dates[9], data={"510300": df}, positions={}, total_asset=1e6,
        rules={"510300": resolve_rule("510300", "etf", load_rules())},
        universe=["510300"])
    b = ctx.bars("510300", 100)
    assert b.index.max() == dates[9]          # 含 today 收盘
    assert len(b) == 10                        # 物理拿不到 today 之后的行
    assert ctx.bars("未知标的", 5).empty


def test_cb_terms_event_truncation():
    terms = pd.DataFrame({"symbol": ["128100"], "stock_code": ["002503"],
                          "conv_price": [1.6], "redeem_status": ["已公告强赎"],
                          "rating": ["CCC"], "maturity": ["2026-03-11"]})
    events = pd.DataFrame({"symbol": ["128100", "128100"],
                           "date": ["2023-05-20", "2023-06-20"],
                           "event": [EVT_REDEEM_ANNOUNCE, "last_trade_day"]})
    cb = build_cb_terms(terms, events)["128100"]
    assert cb.has_event(EVT_REDEEM_ANNOUNCE, until=pd.Timestamp("2023-05-20"))
    assert not cb.has_event(EVT_REDEEM_ANNOUNCE, until=pd.Timestamp("2023-05-19"))
    assert len(cb.events_until(pd.Timestamp("2023-05-31"))) == 1


# ---------------------------------------------------------------- config & registry
def test_config_load():
    s = load_settings()
    assert s.store_root.is_absolute()
    assert s.execution.price_mode in ("close", "open")
    assert 0 < s.risk.max_weight_per_symbol <= 1


def test_registry():
    @register("_dummy_test")
    def _dummy(ctx):
        return {}
    assert get_strategy("_dummy_test") is _dummy
    with pytest.raises(KeyError):
        get_strategy("不存在的策略")
