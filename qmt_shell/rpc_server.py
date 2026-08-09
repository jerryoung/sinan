# coding: utf-8
"""
QMT 内置 Python API 的 socket 转发服务(放进大 QMT 作为模型运行)。

与 shell_strategy(targets 文件薄壳)互补:薄壳是"每日一次的批处理执行",
本服务是"随时可调的 API 通道"——本地程序经 qmt_sdk 以与 QMT 内置 API
完全同名的方式调用行情/交易/账户接口,模拟盘/实盘取决于你把本模型绑到
哪个账号(与薄壳同一原则)。

协议(TCP,仅 127.0.0.1,newline-delimited JSON):
    请求 {"id": 1, "token": "", "fn": "get_trade_detail_data",
          "args": ["8888", "STOCK", "account"], "kwargs": {}}
    响应 {"id": 1, "ok": true, "result": ...}
         {"id": 1, "ok": false, "error": "..."}

fn 解析规则(以此覆盖"全部内置 API",无须逐个枚举):
    "C.xxx"  → ContextInfo 方法(get_full_tick / get_market_data_ex /
               subscribe_quote / get_stock_list_in_sector / get_stock_name /
               get_instrument_detail / get_trading_dates ...)
    其他     → QMT 注入的全局函数(passorder / get_trade_detail_data /
               cancel / timetag_to_datetime ...)
args 中的字符串 "__C__" 会替换为真实 ContextInfo(passorder 末参数约定)。

序列化:基本类型/列表/字典直传;QMT 的 COS 对象(m_* 属性)自动转 dict,
本地 SDK 再还原为属性对象——两端用法与在 QMT 里写代码一致。

安全与线程:只绑定 127.0.0.1;TOKEN 非空时校验。API 调用发生在 socket
线程,常用查询/passorder 实测可用;个别版本对线程敏感的接口如遇问题,
改在 handlebar/定时任务中调用后经文件桥接。
"""
import json
import socket
import threading
import traceback

HOST = "127.0.0.1"
PORT = 58620
TOKEN = ""                    # 非空则每个请求都必须携带一致的 token

_C = None                     # init 时保存的 ContextInfo


# --------------------------------------------------------------------------
# 序列化与调用分发(与 QMT 运行时解耦,便于本地测试)
# --------------------------------------------------------------------------
def to_jsonable(obj, _depth=0):
    """尽力序列化:基本类型直传;COS 对象按属性导出;不可序列化转 str。"""
    if _depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    attrs = [a for a in dir(obj) if a.startswith("m_")]
    if attrs:                                  # QMT COS 对象(m_* 属性)
        return {a: to_jsonable(getattr(obj, a, None), _depth + 1) for a in attrs}
    try:
        return {k: to_jsonable(v, _depth + 1) for k, v in vars(obj).items()}
    except TypeError:
        return str(obj)


def dispatch(namespace, C, fn, args, kwargs):
    """解析 fn 并调用;"__C__" 占位替换为 ContextInfo。"""
    args = [C if a == "__C__" else a for a in (args or [])]
    kwargs = kwargs or {}
    if fn.startswith("C."):
        target = getattr(C, fn[2:], None)
        if target is None:
            raise AttributeError("ContextInfo 无方法: %s" % fn)
    else:
        target = namespace.get(fn)
        if target is None:
            raise NameError("QMT 环境无此函数: %s" % fn)
    return to_jsonable(target(*args, **kwargs))


# --------------------------------------------------------------------------
# socket 服务
# --------------------------------------------------------------------------
def _handle(conn, namespace, C, token):
    buf = b""
    with conn:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                rid = None
                try:
                    req = json.loads(line.decode("utf-8"))
                    rid = req.get("id")
                    if token and req.get("token") != token:
                        raise PermissionError("token 校验失败")
                    result = dispatch(namespace, C, req["fn"],
                                      req.get("args"), req.get("kwargs"))
                    resp = {"id": rid, "ok": True, "result": result}
                except Exception as e:         # noqa: BLE001 逐请求兜底
                    resp = {"id": rid, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)}
                    traceback.print_exc()
                conn.sendall((json.dumps(resp, ensure_ascii=False,
                                         default=str) + "\n").encode("utf-8"))


def serve(namespace, C, host=HOST, port=PORT, token=TOKEN):
    """启动转发服务(每连接一线程);返回监听 socket(测试用)。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)

    def _loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:                    # 监听 socket 已关闭
                return
            threading.Thread(target=_handle, args=(conn, namespace, C, token),
                             daemon=True).start()

    threading.Thread(target=_loop, daemon=True).start()
    print("[rpc] QMT API 转发服务已启动 %s:%s" % (host, port))
    return srv


# --------------------------------------------------------------------------
# QMT 入口
# --------------------------------------------------------------------------
def init(C):
    global _C
    _C = C
    serve(globals(), C)


def handlebar(C):
    pass
