"""QMT socket 桥接端到端:rpc_server 分发/序列化 + qmt_sdk 同名调用还原。

用伪 QMT 命名空间(passorder/get_trade_detail_data/COS 对象)与伪
ContextInfo 在本地起真实 socket 服务,SDK 走完整协议往返。
"""
import socket

import pytest

from qmt_shell import qmt_sdk, rpc_server


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
