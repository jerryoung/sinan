"""
QMT 内置 Python API 的本地 SDK——经 rpc_server 的 socket 转发调用大 QMT。

设计目标:**与 QMT 内置 API 保持同名同形**,把 QMT 文档示例代码几乎原样
搬到本地跑(https://dict.thinktrader.net/innerApi/start_now.html):

    from qmt_shell import qmt_sdk as qmt
    qmt.connect("127.0.0.1", 58620)                       # QMT 侧先运行 rpc_server

    # 账户/持仓(返回对象带 m_* 属性,与 QMT 内一致)
    accs = qmt.get_trade_detail_data("8888888888", "STOCK", "account")
    print(accs[0].m_dBalance)

    # 行情(ContextInfo 方法经 qmt.C 同名调用)
    tick = qmt.C.get_full_tick(["510300.SH"])
    print(tick["510300.SH"]["lastPrice"])
    bars = qmt.C.get_market_data_ex([], ["510300.SH"], period="1d", count=10)

    # 交易(签名与内置 passorder 一致,末参数传 qmt.C 即可)
    qmt.passorder(23, 1101, "8888888888", "510300.SH", 5, -1, 100,
                  "sinan_sdk", 2, "", qmt.C)

覆盖范围:
  - 同名封装:passorder / get_trade_detail_data / cancel / timetag_to_datetime
  - ContextInfo 全部方法:qmt.C.<方法名>(...) 通用代理(get_full_tick、
    get_market_data_ex、subscribe_quote、get_stock_list_in_sector、
    get_stock_name、get_instrument_detail、get_trading_dates …)
  - 其余任何内置全局函数:直接 qmt.<函数名>(...)(模块级 __getattr__ 兜底)
    或 qmt.call("函数名", ...)——协议层通用转发,天然覆盖"全部 API"。

注意:subscribe_quote 的回调函数无法跨进程传递——订阅类接口请在 QMT 侧
脚本内使用,本 SDK 适合查询与交易类调用(拉取式)。
"""
from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace

_DEFAULT = {"host": "127.0.0.1", "port": 58620, "token": "", "timeout": 15.0}


class QmtRpcError(RuntimeError):
    """QMT 侧调用失败(原始异常文本随错误抛回)。"""


class _Client:
    def __init__(self):
        self._sock: socket.socket | None = None
        self._rf = None
        self._lock = threading.Lock()
        self._id = 0
        self.cfg = dict(_DEFAULT)

    def connect(self, host=None, port=None, token=None, timeout=None):
        if host is not None:
            self.cfg["host"] = host
        if port is not None:
            self.cfg["port"] = int(port)
        if token is not None:
            self.cfg["token"] = token
        if timeout is not None:
            self.cfg["timeout"] = float(timeout)
        self.close()
        s = socket.create_connection((self.cfg["host"], self.cfg["port"]),
                                     timeout=self.cfg["timeout"])
        self._sock = s
        self._rf = s.makefile("rb")
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._rf = None, None

    def call(self, fn: str, *args, **kwargs):
        if self._sock is None:
            self.connect()
        with self._lock:
            self._id += 1
            req = {"id": self._id, "token": self.cfg["token"], "fn": fn,
                   "args": ["__C__" if isinstance(a, _CProxy) else a
                            for a in args],
                   "kwargs": kwargs}
            self._sock.sendall((json.dumps(req, ensure_ascii=False) + "\n")
                               .encode("utf-8"))
            line = self._rf.readline()
        if not line:
            self.close()
            raise QmtRpcError("连接已断开(QMT 侧服务是否在运行?)")
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            raise QmtRpcError(resp.get("error", "未知错误"))
        return _revive(resp.get("result"))


def _revive(obj):
    """还原 QMT 对象:含 m_* 键的 dict → 属性对象(与 QMT 内用法一致)。"""
    if isinstance(obj, list):
        return [_revive(x) for x in obj]
    if isinstance(obj, dict):
        out = {k: _revive(v) for k, v in obj.items()}
        if any(k.startswith("m_") for k in out):
            return SimpleNamespace(**out)
        return out
    return obj


class _CProxy:
    """ContextInfo 代理:qmt.C.get_full_tick(...) → 转发 "C.get_full_tick";
    作为参数传入(如 passorder 末参数)时序列化为 "__C__" 占位。"""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: _client.call(f"C.{name}", *a, **kw)


_client = _Client()
C = _CProxy()


def connect(host="127.0.0.1", port=58620, token="", timeout=15.0):
    """连接 QMT 侧 rpc_server;之后所有同名 API 直接调用即可。"""
    return _client.connect(host, port, token, timeout)


def call(fn: str, *args, **kwargs):
    """通用转发:调用任意 QMT 内置全局函数或 "C.方法名"。"""
    return _client.call(fn, *args, **kwargs)


# ---- 同名常用 API(签名与 QMT 内置一致)----------------------------------
def passorder(op_type, order_type, account, order_code, pr_type, price,
              volume, strategy_name="", quick_trade=2, user_order_id="",
              context=C):
    """下单委托(与内置 passorder 同参;context 传 qmt.C 即可)。"""
    return call("passorder", op_type, order_type, account, order_code,
                pr_type, price, volume, strategy_name, quick_trade,
                user_order_id, context)


def get_trade_detail_data(account, account_type, data_type):
    """账户/持仓/委托/成交查询:data_type ∈ account|position|order|deal。"""
    return call("get_trade_detail_data", account, account_type, data_type)


def cancel(order_id, account, account_type="STOCK", context=C):
    """撤单(与内置 cancel 同参)。"""
    return call("cancel", order_id, account, account_type, context)


def timetag_to_datetime(timetag, fmt="%Y-%m-%d %H:%M:%S"):
    """时间戳转换(与内置同名工具函数)。"""
    return call("timetag_to_datetime", timetag, fmt)


def __getattr__(name: str):
    """模块级兜底:任何未封装的内置全局函数按同名直接转发,
    例如 qmt.get_last_volume(...)。"""
    if name.startswith("_"):
        raise AttributeError(name)
    return lambda *a, **kw: call(name, *a, **kw)
