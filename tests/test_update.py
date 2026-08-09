"""Task 3 日更编排测试:主源失败切备源、全源失败告警、质检不通过阻断信号。

FakeSource 继承 DataSource,持有内存 DataFrame;fail 集合指定哪些方法
调用即抛 DataSourceError("*" = 全部)。所有测试不打网络。
"""
import pandas as pd
import pytest

from trend.data.sources.base import DataSource, DataSourceError
from trend.data.store import DataStore
from trend.data.update import UpdateResult, run_daily
from trend.universe.cb_terms import EVT_REDEEM_ANNOUNCE

D = pd.Timestamp("2024-01-10")                          # 周三,交易日
CAL = pd.date_range("2023-12-01", "2024-01-31", freq="B")


def inst_df():
    return pd.DataFrame({
        "symbol":      ["510300", "510500"],
        "name":        ["沪深300ETF", "中证500ETF"],
        "sec_type":    ["etf", "etf"],
        "exchange":    ["SH", "SH"],
        "list_date":   ["2020-01-01", "2020-01-01"],
        "delist_date": [None, None],
        "status":      ["L", "L"],
    })


def bars_df(symbols=("510300", "510500")):
    rows = []
    for i, s in enumerate(symbols):
        c = 10.0 + i
        rows.append({"symbol": s, "date": D, "open": c * 0.995, "high": c * 1.01,
                     "low": c * 0.99, "close": c, "volume": 1e6, "amount": c * 1e6,
                     "adj_factor": 1.0, "pre_close": c * 0.99})
    return pd.DataFrame(rows)


class FakeSource(DataSource):
    """内存假数据源:get_bars 只按 symbols + 日期区间过滤(与 sec_type 无关,
    因 run_daily 每次只传该类型的存续标的)。"""

    def __init__(self, name, instruments=None, bars=None, cb_terms=None,
                 calendar=CAL, fail=()):
        self.name = name
        self._inst = instruments
        self._bars = bars
        self._cb_terms = cb_terms
        self._cal = calendar
        self._fail = set(fail)
        self.calls = []

    def _hit(self, method):
        self.calls.append(method)
        if "*" in self._fail or method in self._fail:
            raise DataSourceError(f"{self.name}.{method} 模拟故障")

    def get_bars(self, symbols, sec_type, start, end):
        self._hit("get_bars")
        if self._bars is None:
            return pd.DataFrame()
        df = self._bars
        m = (df["symbol"].isin(list(symbols))
             & (df["date"] >= pd.Timestamp(start))
             & (df["date"] <= pd.Timestamp(end)))
        return df[m].copy()

    def get_instruments(self, sec_type):
        self._hit("get_instruments")
        if self._inst is None:
            return pd.DataFrame()
        return self._inst[self._inst["sec_type"] == sec_type].copy()

    def get_cb_terms(self):
        self._hit("get_cb_terms")
        return self._cb_terms.copy() if self._cb_terms is not None else pd.DataFrame()

    def get_calendar(self, start, end):
        self._hit("get_calendar")
        idx = pd.DatetimeIndex(self._cal)
        return idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path / "store")


# ---------------------------------------------------------------- 主流程
def test_primary_success(store):
    primary = FakeSource("primary", inst_df(), bars_df())
    backup = FakeSource("backup", fail=("*",))
    res = run_daily(store, [primary, backup], D)
    assert isinstance(res, UpdateResult) and res.ok
    assert res.report is not None and res.report.ok
    out = store.read_bars(start=D, end=D)
    assert set(out["symbol"]) == {"510300", "510500"}
    assert backup.calls == []                    # 主源健康,备源不被触碰
    assert len(store.read_calendar()) > 0
    assert res.source_used["calendar"] == "primary"


def test_failover_to_backup(store):
    primary = FakeSource("primary", fail=("*",))
    backup = FakeSource("backup", inst_df(), bars_df())
    res = run_daily(store, [primary, backup], D)
    assert res.ok
    assert set(store.read_bars(start=D, end=D)["symbol"]) == {"510300", "510500"}
    assert len(primary.calls) > 0                # 主源被尝试过
    assert any("primary" in e for e in res.errors)


def test_partial_failover(store):
    """主源只有 get_bars 挂了 → 备源只需补这一步,其余仍走主源。"""
    primary = FakeSource("primary", inst_df(), bars_df(), fail=("get_bars",))
    backup = FakeSource("backup", inst_df(), bars_df())
    res = run_daily(store, [primary, backup], D)
    assert res.ok
    assert "get_bars" in backup.calls
    assert "get_instruments" not in backup.calls
    assert res.source_used["bars:etf"] == "backup"


def test_all_sources_fail(store):
    msgs = []
    res = run_daily(store, [FakeSource("a", fail=("*",)), FakeSource("b", fail=("*",))],
                    D, notify_fn=msgs.append)
    assert not res.ok and res.report is None
    assert len(msgs) == 1                        # 告警恰好一次


# ---------------------------------------------------------------- 质检阻断
def test_quality_block_and_notify(store):
    """510500 存续但源没给 K 线且无停牌 → 质检不通过:告警 + ok=False(不触发信号)。"""
    msgs = []
    src = FakeSource("primary", inst_df(), bars_df(symbols=("510300",)))
    res = run_daily(store, [src], D, notify_fn=msgs.append)
    assert not res.ok
    assert res.report is not None and not res.report.ok
    assert [(i.symbol, i.kind) for i in res.report.issues] == [("510500", "missing_bar")]
    assert len(msgs) == 1 and "510500" in msgs[0]


def test_non_trading_day(store):
    msgs = []
    src = FakeSource("primary", inst_df(), bars_df())
    res = run_daily(store, [src], pd.Timestamp("2024-01-13"), notify_fn=msgs.append)
    assert res.ok and res.report is None         # 周六:跳过,不告警
    assert len(store.read_bars()) == 0
    assert msgs == []


# ---------------------------------------------------------------- 转债条款
def test_cb_terms_and_redeem_event(store):
    inst = pd.concat([inst_df(), pd.DataFrame({
        "symbol":      ["113001", "128100"],
        "name":        ["平稳转债", "强赎转债"],
        "sec_type":    ["cb", "cb"],
        "exchange":    ["SH", "SZ"],
        "list_date":   ["2020-01-01"] * 2,
        "delist_date": [None, None],
        "status":      ["L", "L"],
    })], ignore_index=True)
    all_bars = pd.concat([bars_df(), bars_df(symbols=("113001", "128100"))],
                         ignore_index=True)
    terms = pd.DataFrame({
        "symbol":        ["113001", "128100"],
        "stock_code":    ["600000", "000001"],
        "conv_price":    [10.0, 5.0],
        "redeem_status": ["不强赎", "已公告强赎"],   # "不强赎" 含"强赎"字样但须排除
    })
    src = FakeSource("primary", inst, all_bars, cb_terms=terms)
    res = run_daily(store, [src], D)
    assert res.ok
    assert len(store.read_cb_terms()) == 2
    ev = store.read_cb_events()
    assert list(ev["symbol"]) == ["128100"]
    assert ev.iloc[0]["event"] == EVT_REDEEM_ANNOUNCE
    assert pd.Timestamp(ev.iloc[0]["date"]) == D
    # 幂等:重复跑不重复记事件
    res2 = run_daily(store, [src], D)
    assert res2.ok and len(store.read_cb_events()) == 1
