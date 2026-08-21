"""显式仿真报单探针：一次提交、按备注确认、可撤才撤、绝不盲重试。"""
from types import SimpleNamespace

import pytest

from scripts.qmt_trade_probe import run_trade_probe


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeClient:
    def __init__(self, orders=None, deals=None, health=None, passorder_error=None):
        self.calls = []
        self.orders = list(orders or [])
        self.deals = list(deals or [])
        self.health = health or {
            "service": "sinan-qmt-rpc", "protocol": 2,
            "account": "80391000", "account_type": "STOCK",
            "allow_trade": True,
        }
        self.passorder_error = passorder_error

    def call(self, fn, *args):
        self.calls.append((fn, args))
        if fn == "rpc.health":
            return self.health
        if fn == "passorder":
            if self.passorder_error:
                raise self.passorder_error
            return None
        if fn == "get_trade_detail_data":
            return self.orders if args[2] == "order" else self.deals
        if fn == "cancel":
            return None
        raise AssertionError(fn)


def _order(remark, status=50, sys_id="O1"):
    return SimpleNamespace(m_strRemark=remark, m_nOrderStatus=status,
                           m_strOrderSysID=sys_id)


def _deal(remark):
    return SimpleNamespace(m_strRemark=remark, m_strTradeID="D1")


def test_probe_requires_exact_account_confirmation_before_side_effect():
    client = FakeClient()

    with pytest.raises(ValueError, match="账号确认不一致"):
        run_trade_probe(client, "wrong", "510300.SH", 100, 4.5)

    assert not any(call[0] == "passorder" for call in client.calls)


def test_probe_submits_once_matches_unique_remark_and_cancels_cancelable_order():
    client = FakeClient()
    clock = FakeClock()
    # runner 生成 remark 后，测试客户端用回调动态回显该备注。
    original_call = client.call

    def call(fn, *args):
        if fn == "get_trade_detail_data" and args[2] == "order":
            remark = [c for c in client.calls if c[0] == "passorder"][0][1][9]
            client.orders = [_order(remark, status=50)]
        return original_call(fn, *args)

    client.call = call

    result = run_trade_probe(
        client, "80391000", "510300.SH", 100, 4.5,
        timeout=2, poll_interval=0.1, clock=clock, sleep=clock.sleep,
    )

    pass_calls = [call for call in client.calls if call[0] == "passorder"]
    cancel_calls = [call for call in client.calls if call[0] == "cancel"]
    assert len(pass_calls) == 1
    assert pass_calls[0][1][:7] == (
        23, 1101, "80391000", "510300.SH", 11, 4.5, 100
    )
    assert pass_calls[0][1][8] == 2
    assert len(pass_calls[0][1][9]) < 24
    assert cancel_calls == [("cancel", ("O1", "80391000", "STOCK", "__C__"))]
    assert result["status"] == "cancel_requested"
    assert result["order_sys_id"] == "O1"


def test_probe_recognizes_terminal_fill_and_does_not_cancel():
    client = FakeClient()
    clock = FakeClock()
    original_call = client.call

    def call(fn, *args):
        if fn == "get_trade_detail_data":
            remark = [c for c in client.calls if c[0] == "passorder"][0][1][9]
            if args[2] == "order":
                client.orders = [_order(remark, status=56)]
            else:
                client.deals = [_deal(remark)]
        return original_call(fn, *args)

    client.call = call
    result = run_trade_probe(
        client, "80391000", "510300.SH", 100, 4.5,
        timeout=2, poll_interval=0.1, clock=clock, sleep=clock.sleep,
    )

    assert result["status"] == "filled"
    assert result["deal_count"] == 1
    assert not any(call[0] == "cancel" for call in client.calls)


def test_probe_timeout_never_resubmits():
    client = FakeClient(orders=[])
    clock = FakeClock()

    result = run_trade_probe(
        client, "80391000", "510300.SH", 100, 4.5,
        timeout=0.3, poll_interval=0.1, clock=clock, sleep=clock.sleep,
    )

    assert result["status"] == "uncertain"
    assert len([call for call in client.calls if call[0] == "passorder"]) == 1


def test_probe_refuses_readonly_rpc():
    client = FakeClient(health={
        "service": "sinan-qmt-rpc", "protocol": 2,
        "account": "80391000", "account_type": "STOCK",
        "allow_trade": False,
    })
    with pytest.raises(ValueError, match="交易转发"):
        run_trade_probe(client, "80391000", "510300.SH", 100, 4.5)
    assert not any(call[0] == "passorder" for call in client.calls)
