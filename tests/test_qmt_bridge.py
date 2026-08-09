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
