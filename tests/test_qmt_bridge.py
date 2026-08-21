"""QMT socket 桥接端到端:sinan_qmt(统一脚本)分发/序列化 + qmt_sdk 同名还原。

用伪 QMT 命名空间(passorder/get_trade_detail_data/COS 对象)与伪
ContextInfo 在本地起真实 socket 服务,SDK 走完整协议往返。
"""
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from qmt_shell import qmt_sdk, sinan_qmt as rpc_server
from sinan.config import (LiveProfileCfg, LiveProfilesCfg, QmtExecutionCfg,
                          QmtRpcCfg)


class _BadOrderTag:
    """QMT 实盘委托对象中的 m_xtTag 是不可转出的 C++ shared_ptr。"""

    m_strRemark = "probe#20260821#1"
    m_strOrderSysID = "12345"

    @property
    def m_xtTag(self):
        raise TypeError("No to_python converter for CXtOrderTag")


def test_to_jsonable_skips_unconvertible_qmt_attribute():
    out = rpc_server.to_jsonable(_BadOrderTag())

    assert out["m_strRemark"] == "probe#20260821#1"
    assert out["m_strOrderSysID"] == "12345"
    assert "m_xtTag" not in out


def test_qmt_iso_parser_is_python36_compatible():
    value = rpc_server._parse_iso_datetime("2026-08-21T14:35:01")

    assert value.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-21 14:35:01"
    source = Path(rpc_server.__file__).read_text(encoding="utf-8")
    assert "datetime.fromisoformat" not in source


class _PeerAbortConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def recv(self, _size):
        raise ConnectionAbortedError(10053, "连接已由主机软件中止")

    def close(self):
        pass


def test_rpc_handler_treats_peer_abort_as_normal_disconnect(capsys):
    rpc_server._handle(
        _PeerAbortConnection(), ("127.0.0.1", 1), {}, object(), "", True, []
    )

    assert "Traceback" not in capsys.readouterr().err


def test_sdk_close_releases_reader_before_socket_shutdown():
    events = []

    class Reader:
        def close(self):
            events.append("reader.close")

    class Socket:
        def shutdown(self, how):
            events.append(("socket.shutdown", how))

        def close(self):
            events.append("socket.close")

    client = qmt_sdk._Client()
    client._rf = Reader()
    client._sock = Socket()

    client.close()

    assert events == [
        "reader.close",
        ("socket.shutdown", socket.SHUT_RDWR),
        "socket.close",
    ]
    assert client._rf is None and client._sock is None


def test_qmt_api_request_runs_only_when_pump_drains_queue():
    rpc_server._reset_rpc_queue_for_test()
    seen = []
    result = {}
    namespace = {
        "get_trade_detail_data": lambda *args: seen.append(args) or ["ok"]
    }
    C = object()

    worker = threading.Thread(
        target=lambda: result.update(
            value=rpc_server._submit_api_request(
                namespace, C, "get_trade_detail_data",
                ["a", "STOCK", "order"], {}, True, 1.0,
            )
        )
    )
    worker.start()
    time.sleep(0.02)

    assert seen == []
    rpc_server.do_rpc_pump(C)
    worker.join(1)
    assert result["value"] == ["ok"]
    assert seen == [("a", "STOCK", "order")]


def test_qmt_api_request_times_out_without_pump():
    rpc_server._reset_rpc_queue_for_test()

    with pytest.raises(TimeoutError, match="超时"):
        rpc_server._submit_api_request(
            {}, object(), "get_trade_detail_data", [], {}, True, 0.01
        )


def test_rpc_health_v2_is_direct_and_advertises_current_capability():
    rpc_server._reset_rpc_queue_for_test()

    health = rpc_server.dispatch({}, object(), "rpc.health", [], {}, True)

    assert health["protocol"] == 2
    assert health["capabilities"] == ["qmt_api_queue"]
    assert rpc_server._RPC_API_QUEUE.empty()


@pytest.mark.parametrize("fn", ["eval", "open", "C.set_account", "C.no_such_method"])
def test_rpc_method_allowlist_rejects_unapproved_names(fn):
    with pytest.raises(PermissionError, match="不允许"):
        rpc_server._validate_rpc_method(fn)


@pytest.mark.parametrize("fn", [
    "get_trade_detail_data", "get_last_order_id", "get_value_by_order_id",
    "timetag_to_datetime", "passorder", "cancel", "cancel_task",
    "C.get_full_tick", "C.get_stock_name", "C.get_market_data_ex",
    "C.get_trading_dates", "C.get_stock_list_in_sector",
    "C.get_instrument_detail",
])
def test_rpc_method_allowlist_accepts_repository_contract(fn):
    rpc_server._validate_rpc_method(fn)


class _OversizedRequestConnection:
    def __init__(self):
        self.sent = []
        self._chunks = [b"x" * (1024 * 1024 + 1) + b"\n"]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, value):
        self.sent.append(value)

    def close(self):
        pass


def test_rpc_rejects_request_line_over_one_mib_without_response():
    conn = _OversizedRequestConnection()

    rpc_server._handle(conn, ("127.0.0.1", 1), {}, object(), "", True, [])

    assert conn.sent == []


def test_qmt_rpc_defaults_are_unconfigured_and_trade_enabled():
    """仓库不携带用户网络信息；QMT RPC 默认开启交易能力。"""
    assert rpc_server.RPC_TOKEN == ""
    assert rpc_server.RPC_ALLOW_IPS == []
    assert rpc_server.RPC_ALLOW_TRADE is True


def test_load_local_config_creates_safe_default_when_file_missing(tmp_path):
    """删除配置文件后再启动，必须自动落一份关闭 RPC 的可编辑安全配置。"""
    path = tmp_path / "config" / "qmt.json"

    cfg = rpc_server.load_local_config(str(path))

    assert path.exists()
    assert cfg["share_dir"] == r"C:\sinan\var\runtime"
    assert cfg["rpc"] == {
        "enable": False,
        "host": "0.0.0.0",
        "port": 58620,
        "token": "",
        "allow_trade": True,
        "allow_ips": [],
    }
    assert cfg["live_push"] == {"enable": True, "period": "5nSecond"}
    assert json.loads(path.read_text(encoding="utf-8")) == cfg


def test_load_local_config_reads_all_machine_values_from_one_file(tmp_path):
    """替换脚本后，服务器路径、Token 和白名单仍全部来自同一 JSON。"""
    path = tmp_path / "qmt.json"
    path.write_text(json.dumps({
        "share_dir": r"D:\sinan\runtime",
        "rpc": {
            "enable": True,
            "host": "0.0.0.0",
            "port": 60001,
            "token": "  " + "x" * 32 + "\n",
            "allow_trade": False,
            "allow_ips": ["120.245.101.210", "100.64.0.0/10"],
        },
        "live_push": {"enable": False, "period": "10nSecond"},
    }), encoding="utf-8")

    cfg = rpc_server.load_local_config(str(path))

    assert cfg["share_dir"] == r"D:\sinan\runtime"
    assert cfg["rpc"]["port"] == 60001
    assert cfg["rpc"]["token"] == "x" * 32
    assert cfg["rpc"]["allow_trade"] is False
    assert cfg["rpc"]["allow_ips"] == [
        "120.245.101.210", "100.64.0.0/10"
    ]
    assert cfg["live_push"] == {"enable": False, "period": "10nSecond"}


def test_load_local_config_accepts_windows_powershell_utf8_bom(tmp_path):
    """Windows PowerShell 5 写出的 UTF-8 BOM 配置必须可直接读取。"""
    path = tmp_path / "qmt.json"
    payload = {"rpc": {"enable": False}}
    path.write_bytes(json.dumps(payload).encode("utf-8-sig"))

    cfg = rpc_server.load_local_config(str(path))

    assert cfg["rpc"]["enable"] is False
    assert cfg["share_dir"] == r"C:\sinan\var\runtime"


@pytest.mark.parametrize("payload,match", [
    ({"rpc": {"port": 70000}}, "rpc.port"),
    ({"rpc": {"allow_ips": ["错误IP"]}}, "rpc.allow_ips"),
    ({"rpc": {"enable": True, "host": "0.0.0.0",
              "token": "short", "allow_ips": ["1.2.3.4"]}}, "至少32位"),
    ({"live_push": {"enable": "yes"}}, "live_push.enable"),
    ({"live_push": {"period": "随便写"}}, "live_push.period"),
])
def test_load_local_config_rejects_invalid_values(tmp_path, payload, match):
    """错误配置必须在启动时定位，不得留到收到 RPC 请求时才暴露。"""
    path = tmp_path / "qmt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(rpc_server.QmtConfigError, match=match):
        rpc_server.load_local_config(str(path))


def test_load_local_config_rejects_invalid_json_without_leaking_content(tmp_path):
    """损坏配置的异常只报告路径，不复述可能含 Token 的文件正文。"""
    path = tmp_path / "qmt.json"
    path.write_text('{"rpc":{"token":"TOP_SECRET"}', encoding="utf-8")

    with pytest.raises(rpc_server.QmtConfigError) as exc:
        rpc_server.load_local_config(str(path))

    assert str(path) in str(exc.value)
    assert "TOP_SECRET" not in str(exc.value)


def test_windows_rpc_socket_uses_exclusive_address(monkeypatch):
    """Windows QMT 必须独占端口,避免多个模型被 SO_REUSEADDR 静默分流。"""
    fake = Mock()
    monkeypatch.setattr(rpc_server.os, "name", "nt")
    monkeypatch.setattr(rpc_server.socket, "socket", Mock(return_value=fake))

    assert rpc_server._make_server_socket() is fake
    fake.setsockopt.assert_called_once_with(
        socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 4), 1
    )


def test_qmt_stop_callback_closes_rpc_server(monkeypatch):
    """QMT 停止模型时调用 stop,必须释放后台监听端口。"""
    server = Mock()
    monkeypatch.setattr(rpc_server, "_RPC_SERVER", server)

    rpc_server.stop(_FakeC())

    server.close.assert_called_once_with()
    assert rpc_server._RPC_SERVER is None


class _COS:                                     # 模拟 QMT 账户对象(m_* 属性)
    def __init__(self):
        self.m_dBalance = 100000.0
        self.m_dAvailable = 60000.0
        self.m_strInstrumentID = "510300"


class _FakeC:                                   # 模拟 ContextInfo
    def __init__(self):
        self.schedules = []

    def run_time(self, name, period, start, market):
        self.schedules.append((name, period, start, market))

    def get_full_tick(self, codes):
        return {c: {"lastPrice": 4.75} for c in codes}

    def get_stock_name(self, code):
        return "沪深300ETF"


def _runtime_config(**rpc_overrides):
    rpc = {
        "enable": True,
        "host": "127.0.0.1",
        "port": 60001,
        "token": "x" * 32,
        "allow_trade": True,
        "allow_ips": ["127.0.0.1"],
    }
    rpc.update(rpc_overrides)
    return {
        "share_dir": r"D:\sinan\runtime",
        "rpc": rpc,
        "live_push": {"enable": False, "period": "10nSecond"},
    }


def _restore_runtime_globals_after_test(monkeypatch):
    """init 会应用进程级启动快照；逐项登记以便 pytest 在用例后还原。"""
    for name in (
        "SHARE_DIR", "RPC_ENABLE", "RPC_HOST", "RPC_PORT", "RPC_TOKEN",
        "RPC_ALLOW_TRADE", "RPC_ALLOW_IPS", "LIVE_PUSH_ENABLE",
        "LIVE_PUSH_PERIOD", "_RPC_SERVER",
    ):
        monkeypatch.setattr(rpc_server, name, getattr(rpc_server, name))


def test_init_applies_local_config_to_runtime(monkeypatch):
    """启动必须显式使用 JSON 快照，不能继续吃函数定义时捕获的旧常量。"""
    C = _FakeC()
    server = Mock()
    _restore_runtime_globals_after_test(monkeypatch)
    monkeypatch.setattr(rpc_server, "load_local_config", _runtime_config)
    monkeypatch.setattr(rpc_server, "serve", Mock(return_value=server))

    rpc_server.init(C)

    assert rpc_server.SHARE_DIR == r"D:\sinan\runtime"
    assert [item[0] for item in C.schedules] == [
        "do_rebalance", "do_snapshot", "do_rpc_pump"
    ]
    assert C.schedules[-1][1] == "1nSecond"
    rpc_server.serve.assert_called_once_with(
        rpc_server.__dict__, C,
        host="127.0.0.1", port=60001, token="x" * 32,
        allow_trade=True, allow_ips=["127.0.0.1"],
    )
    assert rpc_server._RPC_SERVER is server


def test_init_keeps_core_schedules_when_local_config_is_invalid(
        monkeypatch, capsys):
    """配置损坏只能关闭 RPC，不能让当日调仓和快照调度消失。"""
    C = _FakeC()
    _restore_runtime_globals_after_test(monkeypatch)
    monkeypatch.setattr(
        rpc_server, "load_local_config",
        Mock(side_effect=rpc_server.QmtConfigError("rpc.port 非法")),
    )
    monkeypatch.setattr(rpc_server, "serve", Mock())

    rpc_server.init(C)

    assert [item[0] for item in C.schedules] == [
        "do_rebalance", "do_snapshot", "do_live_push"
    ]
    rpc_server.serve.assert_not_called()
    assert rpc_server._RPC_SERVER is None
    assert "RPC 未启动" in capsys.readouterr().out


def test_init_keeps_core_schedules_when_rpc_port_is_occupied(
        monkeypatch, capsys):
    """端口占用应变成局部告警，而不是从 init 向 QMT 抛出 WinError 10048。"""
    C = _FakeC()
    _restore_runtime_globals_after_test(monkeypatch)
    monkeypatch.setattr(rpc_server, "load_local_config", _runtime_config)
    monkeypatch.setattr(
        rpc_server, "serve", Mock(side_effect=OSError(10048, "端口已占用"))
    )

    rpc_server.init(C)

    assert [item[0] for item in C.schedules] == ["do_rebalance", "do_snapshot"]
    assert rpc_server._RPC_SERVER is None
    output = capsys.readouterr().out
    assert "RPC 未启动" in output
    assert "10048" in output


ORDERS = []


def _fake_passorder(op, order_type, account, code, pr_type, price, volume,
                    strategy, quick, uid, C):
    ORDERS.append({"op": op, "code": code, "volume": volume,
                   "C_is_ctx": isinstance(C, _FakeC)})


def _fake_get_trade_detail_data(account, acc_type, kind):
    return [_COS()]


def _run_test_pump(C, stopped):
    """测试线程模拟 QMT 的 1 秒策略线程定时回调。"""
    while not stopped.is_set():
        rpc_server.do_rpc_pump(C)
        stopped.wait(0.001)


def _start_test_pump(C):
    stopped = threading.Event()
    pump = threading.Thread(
        target=_run_test_pump, args=(C, stopped), daemon=True
    )
    pump.start()
    return stopped, pump


@pytest.fixture(scope="module")
def bridge():
    ns = {"passorder": _fake_passorder,
          "get_trade_detail_data": _fake_get_trade_detail_data,
          "timetag_to_datetime": lambda t, f: "2026-08-10 14:45:00"}
    with socket.socket() as probe:              # 找一个空闲端口
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    C = _FakeC()
    srv = rpc_server.serve(ns, C, host="127.0.0.1", port=port,
                           token="t0k", allow_trade=True)
    stopped, pump = _start_test_pump(C)
    qmt_sdk.connect("127.0.0.1", port, token="t0k")
    yield
    qmt_sdk._client.close()
    srv.close()
    stopped.set()
    pump.join(1)


def test_named_api_and_cos_revival(bridge):
    accs = qmt_sdk.get_trade_detail_data("8888", "STOCK", "account")
    assert accs[0].m_dBalance == 100000.0       # m_* 属性还原,与 QMT 内一致
    assert accs[0].m_strInstrumentID == "510300"


def test_health_reports_rpc_runtime_without_calling_qmt_api(bridge):
    health = qmt_sdk.health()
    assert health["service"] == "sinan-qmt-rpc"
    assert health["protocol"] == 2
    assert health["capabilities"] == ["qmt_api_queue"]
    assert health["allow_trade"] is True
    assert health["account_type"] == "STOCK"


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
    with pytest.raises(qmt_sdk.QmtRpcError, match="不允许"):
        qmt_sdk.call("not_exist_fn")
    with pytest.raises(qmt_sdk.QmtRpcError, match="不允许"):
        qmt_sdk.C.no_such_method()


def test_token_rejected():
    ns = {}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = rpc_server.serve(ns, _FakeC(), host="127.0.0.1", port=port,
                           token="secret")
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
    C = _FakeC()
    srv = rpc_server.serve(ns, C, host="127.0.0.1", port=port,
                           token="t", allow_trade=False)
    stopped, pump = _start_test_pump(C)
    cli = qmt_sdk._Client()
    cli.connect("127.0.0.1", port, token="t")
    assert cli.call("get_trade_detail_data", "a", "STOCK",
                    "account")[0].m_dBalance == 100000.0
    with pytest.raises(qmt_sdk.QmtRpcError, match="只读通道"):
        cli.call("passorder", 23, 1101, "a", "x", 5, -1, 100, "", 2, "", "__C__")
    cli.close()
    srv.close()
    stopped.set()
    pump.join(1)


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
    srv = rpc_server.serve({}, _FakeC(), host="127.0.0.1", port=port, token="t",
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
