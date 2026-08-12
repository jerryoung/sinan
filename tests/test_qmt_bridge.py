"""QMT socket 桥接端到端:sinan_qmt(统一脚本)分发/序列化 + qmt_sdk 同名还原。

用伪 QMT 命名空间(passorder/get_trade_detail_data/COS 对象)与伪
ContextInfo 在本地起真实 socket 服务,SDK 走完整协议往返。
"""
import socket

import pytest

from qmt_shell import qmt_sdk, sinan_qmt as rpc_server
from sinan.config import (LiveProfileCfg, LiveProfilesCfg, QmtExecutionCfg,
                          QmtRpcCfg)


class _COS:                                     # 模拟 QMT 账户对象(m_* 属性)
    def __init__(self):
        self.m_dBalance = 100000.0
        self.m_dAvailable = 60000.0
        self.m_strInstrumentID = "510300"


class _FakeC:                                   # 模拟 ContextInfo
    def get_full_tick(self, codes):
        return {c: {"lastPrice": 4.75} for c in codes}

    def get_stock_name(self, code):
        return "沪深300ETF"


ORDERS = []


def _fake_passorder(op, order_type, account, code, pr_type, price, volume,
                    strategy, quick, uid, C):
    ORDERS.append({"op": op, "code": code, "volume": volume,
                   "C_is_ctx": isinstance(C, _FakeC)})


def _fake_get_trade_detail_data(account, acc_type, kind):
    return [_COS()]


@pytest.fixture(scope="module")
def bridge():
    ns = {"passorder": _fake_passorder,
          "get_trade_detail_data": _fake_get_trade_detail_data,
          "timetag_to_datetime": lambda t, f: "2026-08-10 14:45:00"}
    with socket.socket() as probe:              # 找一个空闲端口
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = rpc_server.serve(ns, _FakeC(), port=port, token="t0k")
    qmt_sdk.connect("127.0.0.1", port, token="t0k")
    yield
    qmt_sdk._client.close()
    srv.close()


def test_named_api_and_cos_revival(bridge):
    accs = qmt_sdk.get_trade_detail_data("8888", "STOCK", "account")
    assert accs[0].m_dBalance == 100000.0       # m_* 属性还原,与 QMT 内一致
    assert accs[0].m_strInstrumentID == "510300"


def test_context_proxy_and_passorder(bridge):
    tick = qmt_sdk.C.get_full_tick(["510300.SH"])
    assert tick["510300.SH"]["lastPrice"] == 4.75
    assert qmt_sdk.C.get_stock_name("510300.SH") == "沪深300ETF"
    qmt_sdk.passorder(23, 1101, "8888", "510300.SH", 5, -1, 100,
                      "test", 2, "", qmt_sdk.C)
    assert ORDERS and ORDERS[-1]["C_is_ctx"]    # "__C__" 占位换回真 ContextInfo
    assert ORDERS[-1]["volume"] == 100


def test_generic_fallback_and_errors(bridge):
    # 模块级 __getattr__ 兜底:未显式封装的函数同名转发
    assert qmt_sdk.timetag_to_datetime(0, "%Y") == "2026-08-10 14:45:00"
    with pytest.raises(qmt_sdk.QmtRpcError, match="无此函数"):
        qmt_sdk.call("not_exist_fn")
    with pytest.raises(qmt_sdk.QmtRpcError, match="无方法"):
        qmt_sdk.C.no_such_method()


def test_token_rejected():
    ns = {}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = rpc_server.serve(ns, _FakeC(), port=port, token="secret")
    bad = qmt_sdk._Client()
    bad.connect("127.0.0.1", port, token="wrong")
    with pytest.raises(qmt_sdk.QmtRpcError, match="token"):
        bad.call("anything")
    bad.close()
    srv.close()


def test_readonly_channel_blocks_trade_allows_query():
    """ALLOW_TRADE=False:交易函数拒绝、查询照常——远端只读的最小权限。"""
    ns = {"passorder": _fake_passorder,
          "get_trade_detail_data": _fake_get_trade_detail_data}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = rpc_server.serve(ns, _FakeC(), port=port, token="t", allow_trade=False)
    cli = qmt_sdk._Client()
    cli.connect("127.0.0.1", port, token="t")
    assert cli.call("get_trade_detail_data", "a", "STOCK",
                    "account")[0].m_dBalance == 100000.0
    with pytest.raises(qmt_sdk.QmtRpcError, match="只读通道"):
        cli.call("passorder", 23, 1101, "a", "x", 5, -1, 100, "", 2, "", "__C__")
    cli.close()
    srv.close()


def test_remote_bind_requires_token_and_allowlist():
    """非 127.0.0.1 绑定必须同时配 token + IP 白名单,缺一拒绝启动。"""
    with pytest.raises(ValueError, match="TOKEN"):
        rpc_server.serve({}, _FakeC(), host="0.0.0.0", port=0, token="")
    with pytest.raises(ValueError, match="ALLOW_IPS"):
        rpc_server.serve({}, _FakeC(), host="0.0.0.0", port=0, token="t",
                         allow_ips=[])


def test_ip_allowlist_cidr_and_rejection():
    """白名单支持单 IP 与 CIDR;不在名单的连接在握手层被断开。"""
    assert rpc_server.ip_allowed("100.101.102.103", ["100.64.0.0/10"])
    assert rpc_server.ip_allowed("1.2.3.4", ["1.2.3.4"])
    assert not rpc_server.ip_allowed("8.8.8.8", ["100.64.0.0/10", "1.2.3.4"])
    assert not rpc_server.ip_allowed("8.8.8.8", ["写错的条目"])   # 坏配置不放行
    # 连接级拒绝:白名单不含 127.0.0.1 → 本地连接被立即断开
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = rpc_server.serve({}, _FakeC(), port=port, token="t",
                           allow_ips=["10.9.9.9"])
    cli = qmt_sdk._Client()
    cli.connect("127.0.0.1", port, token="t")
    with pytest.raises(qmt_sdk.QmtRpcError, match="断开"):
        cli.call("anything")
    cli.close()
    srv.close()


def test_connect_from_settings_uses_default_live_profile_rpc(monkeypatch, tmp_path):
    import sinan.config as config_module

    profiles = LiveProfilesCfg(
        default="remote_qmt",
        profiles={
            "remote_qmt": LiveProfileCfg(
                name="远端 QMT",
                qmt=QmtExecutionCfg(
                    rpc=QmtRpcCfg(host="100.64.0.8", port=60001, timeout=9.0),
                ),
            ),
        },
    )
    seen = {}
    monkeypatch.setattr(config_module, "load_live_profiles", lambda: profiles)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        qmt_sdk,
        "connect",
        lambda host, port, token, timeout: seen.update(
            host=host, port=port, token=token, timeout=timeout
        ),
    )

    qmt_sdk.connect_from_settings()

    assert seen == {
        "host": "100.64.0.8",
        "port": 60001,
        "token": "",
        "timeout": 9.0,
    }


# ---------------- 多策略共账号:备注归因 + 虚拟账本 ----------------
def test_remark_roundtrip():
    r = rpc_server.make_remark("combo_x2", "20260810", 3)
    assert r == "combo_x2#20260810#3"
    strategy, ext = rpc_server.parse_remark(r)
    assert strategy == "combo_x2" and ext == ["20260810", "3"]
    assert rpc_server.parse_remark("手工下单") == (None, [])
    assert rpc_server.parse_remark("") == (None, [])


def test_plan_orders_diff_and_sell_first():
    """差额按策略自身账本计算;卖单在前释放现金;手数向下取整。"""
    ledger = {"cash": 50000.0, "pos": {"510300": 10000, "159941": 0}}
    prices = {"510300": 5.0, "159941": 2.0, "518880": 8.0}
    # total = 50000 + 10000×5 = 100000;目标:300 减到 30%、新开 518880 40%
    orders = rpc_server.plan_orders(
        {"510300": 0.3, "518880": 0.4}, ledger, prices, lot=100)
    assert orders[0] == ("510300", "sell", 4000, 5.0)     # 卖单在前
    assert ("518880", "buy", 5000, 8.0) in orders         # 40000/8=5000 股
    # 未持有且目标为 0 的标的不产生委托
    assert all(o[0] != "159941" for o in orders)


def test_ledger_seed_and_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    led = rpc_server.load_ledger("s1", 100000.0)
    assert led == {"cash": 100000.0, "pos": {}}            # 首次以 capital 开账
    led["pos"]["510300"] = 2000
    led["cash"] -= 2000 * 5.0
    rpc_server.save_ledger("s1", led)
    led2 = rpc_server.load_ledger("s1", 999.0)             # 已有账本忽略 capital
    assert led2["cash"] == 90000.0 and led2["pos"]["510300"] == 2000
