# coding: utf-8
"""
QMT 内置 Python API 的 socket 转发服务(放进大 QMT 作为模型运行)。

与 shell_strategy(targets 文件薄壳)互补:薄壳是"每日一次的批处理执行",
本服务是"随时可调的 API 通道"——本地/远端程序经 qmt_sdk 以与 QMT 内置
API 完全同名的方式调用行情/交易/账户接口,模拟盘/实盘取决于你把本模型
绑到哪个账号(与薄壳同一原则)。

── 远程访问安全模型(Windows ECS 部署必读)────────────────────────────
协议是明文 TCP:**传输安全交给隧道,应用层 token 是第二道锁,不是唯一的锁**。
  推荐 A(SSH 隧道):HOST 保持 127.0.0.1,ECS 开 OpenSSH,本地
      ssh -N -L 58620:127.0.0.1:58620 user@ecs —— 零暴露,加密+认证齐全;
  推荐 B(Tailscale/WireGuard):两端装 Tailscale,HOST 绑定 ECS 的
      100.x 虚拟网卡 IP + 强 TOKEN —— 私网可达,公网不可见;
  下策 C(直接暴露公网端口):仅当 ECS 安全组已收紧到你的出口 IP,
      且 TOKEN ≥32 位随机、ALLOW_TRADE=False(只读)时才可接受。
本服务强制:非 127.0.0.1 绑定必须配置非空 TOKEN,否则拒绝启动;
token 恒时比较防时序侧信道;ALLOW_IPS 白名单;ALLOW_TRADE=False 时
拒绝交易类函数(passorder/cancel),数据查询照常——远端只读的最小权限。

协议(newline-delimited JSON):
    请求 {"id": 1, "token": "", "fn": "get_trade_detail_data",
          "args": ["8888", "STOCK", "account"], "kwargs": {}}
    响应 {"id": 1, "ok": true, "result": ...} / {"id":1,"ok":false,"error":"..."}
fn 解析(以此覆盖"全部内置 API",无须逐个枚举):
    "C.xxx" → ContextInfo 方法;其他 → QMT 注入的全局函数。
args 中的 "__C__" 占位会替换为真实 ContextInfo(passorder 末参数约定)。
序列化:基本类型直传;QMT COS 对象(m_* 属性)转 dict,SDK 侧还原。
线程:API 调用发生在 socket 线程,常用查询/passorder 实测可用;个别版本
线程敏感的接口如遇问题,改在定时任务中调用后经文件桥接。
"""
import hmac
import json
import socket
import threading
import traceback

HOST = "127.0.0.1"      # 远程访问优先走 SSH 隧道(保持 127.0.0.1);
PORT = 58620            # 用 Tailscale 时改绑 100.x 虚拟网卡 IP
TOKEN = ""              # 非 127.0.0.1 绑定必须非空;建议 ≥32 位随机串
ALLOW_TRADE = True      # False = 只读通道:拒绝交易类函数,查询照常
ALLOW_IPS = []          # 额外 IP 白名单(空=不限;直接暴露时务必配置)

_TRADE_FNS = {"passorder", "cancel", "cancel_task"}   # 交易类函数名(按需扩展)
_C = None


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


def dispatch(namespace, C, fn, args, kwargs, allow_trade=True):
    """解析 fn 并调用;"__C__" 占位替换为 ContextInfo;只读模式拒绝交易函数。"""
    if not allow_trade and fn in _TRADE_FNS:
        raise PermissionError("只读通道(ALLOW_TRADE=False),拒绝交易函数: %s" % fn)
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


def _token_ok(supplied, token):
    """恒时比较,防时序侧信道;token 未配置时放行(仅限本机绑定)。"""
    if not token:
        return True
    return hmac.compare_digest(str(supplied or ""), token)


# --------------------------------------------------------------------------
# socket 服务
# --------------------------------------------------------------------------
def _handle(conn, addr, namespace, C, token, allow_trade, allow_ips):
    if allow_ips and addr[0] not in allow_ips:
        print("[rpc] 拒绝非白名单来源:", addr[0])
        conn.close()
        return
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
                    if not _token_ok(req.get("token"), token):
                        raise PermissionError("token 校验失败")
                    result = dispatch(namespace, C, req["fn"],
                                      req.get("args"), req.get("kwargs"),
                                      allow_trade=allow_trade)
                    resp = {"id": rid, "ok": True, "result": result}
                except Exception as e:         # noqa: BLE001 逐请求兜底
                    resp = {"id": rid, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)}
                    traceback.print_exc()
                conn.sendall((json.dumps(resp, ensure_ascii=False,
                                         default=str) + "\n").encode("utf-8"))


def serve(namespace, C, host=HOST, port=PORT, token=TOKEN,
          allow_trade=ALLOW_TRADE, allow_ips=None):
    """启动转发服务(每连接一线程);返回监听 socket(测试用)。

    安全强制:非 127.0.0.1 绑定必须配置非空 token,否则拒绝启动。
    """
    if host not in ("127.0.0.1", "localhost") and not token:
        raise ValueError("非本机绑定(%s)必须配置非空 TOKEN——远程访问请优先"
                         "走 SSH 隧道/Tailscale,token 只是第二道锁" % host)
    allow_ips = list(allow_ips if allow_ips is not None else ALLOW_IPS)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)

    def _loop():
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:                    # 监听 socket 已关闭
                return
            threading.Thread(target=_handle,
                             args=(conn, addr, namespace, C, token,
                                   allow_trade, allow_ips),
                             daemon=True).start()

    threading.Thread(target=_loop, daemon=True).start()
    print("[rpc] QMT API 转发服务已启动 %s:%s(trade=%s, 白名单=%s)"
          % (host, port, allow_trade, allow_ips or "不限"))
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
