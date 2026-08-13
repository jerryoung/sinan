# coding: utf-8
"""
司南 ECS 统一脚本——放进大 QMT 的唯一一个模型,三件事全包:

    ① 14:45 执行当日全部策略的 targets(多策略共账号,下单备注归因)
    ② 15:05 按成交回报修正并回写各策略 fills(收盘兜底)
    ③ 常驻 socket 转发 QMT API(本地 qmt_sdk 同名调用;安全模型见下)
    ④ 每 5 秒推送实盘信息:新成交即时回写 fills,账户快照写
       state/qmt_live.json

── 配置最小化(第一性原理)──────────────────────────────────────────
你在 QMT 把本模型绑定到 模拟/实盘 账号时已经选过 账号类型 与 资金账号,
脚本直接从运行环境读取(account/accountType 注入,ContextInfo 属性兜底),
不需要再填一遍。**必填配置只有一项:SHARE_DIR(与本地同步的目录)**;
其余全部有合理缺省。

── 多策略共账号:备注归因 + 策略虚拟账本 ─────────────────────────────
每笔委托的投资备注为「策略ID#日期#序号」(可再扩展 # 字段)。脚本为每个
策略维护一本虚拟账本(现金+持仓,SHARE_DIR/state/ledger_{策略}.json,
首次以 targets 的 capital 开账):下单差额 = 策略自己的目标 − 自己的账本,
互不读写对方持仓;15:05 查询当日成交(deal),按备注归回各策略、以实际
成交价重算账本与 fills。多个策略同持一只标的、甚至反向调仓都不会串账
——代价是极少数日子里可能左手换右手多付一次费用,换来的是归因精确。

── 安全(远程访问必读,详见 docs/qmt-deploy.md)─────────────────────
RPC 明文 TCP:传输安全靠 SSH 隧道/Tailscale;非 127.0.0.1 绑定强制
非空 TOKEN + 非空 IP 白名单(支持 CIDR),缺一拒绝启动;
RPC_ALLOW_TRADE=False 时转发通道只读(targets 批处理执行不受影响)。

本文件在本地不可执行(passorder 等由 QMT 注入),部署=整文件粘贴;
校验和算法与 sinan/live/targets.py 逐字节一致(文件桥接解耦的全部代价)。
"""
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import traceback
from datetime import datetime

# ══════════════ 唯一必填:与本地同步的共享目录 ══════════════
SHARE_DIR = r"C:\sinan\var\runtime"

# ---- 以下全部可保持缺省 --------------------------------------------------
# 只服务指定策略(策略ID = 司南策略配置的 name,面板右上可一键复制);
# 空 = 执行共享目录里发现的当日全部策略 targets
STRATEGIES = []
TRADE_MODE = "auto"          # "sim"/"real" 人工声明;"auto" 尽力探测,失败 unknown
LOT = 100                    # ETF/股票一手;可转债改 10
MAX_AGE_HOURS = 8.0          # targets 时效
ALGO_DEFAULT = {"quote_mode": "latest",   # latest=最新价 / limit=限价(+偏移)
                "price_offset": 0.002, "max_order_qty": 100 * LOT}
_PR_TYPE = {"latest": 5, "limit": 11}

RPC_ENABLE = True            # 关掉则只做 targets 批处理
RPC_HOST = "0.0.0.0"         # 远程监听;必须同时填写 TOKEN 和 ALLOW_IPS
RPC_PORT = 58620
RPC_TOKEN = ""               # 非 127.0.0.1 绑定必须非空(≥32 位随机)
RPC_ALLOW_TRADE = False      # 调试默认只读;targets 定时执行不受影响
RPC_ALLOW_IPS = []           # 非本机绑定必须非空:单 IP 或 CIDR(100.64.0.0/10)

LIVE_PUSH_ENABLE = True
LIVE_PUSH_PERIOD = "5nSecond"

_TRADE_FNS = {"passorder", "cancel", "cancel_task"}
_C = None
_ACCOUNT = {"id": "", "type": "STOCK"}
_MORNING_LEDGERS = {}        # 用当日成交重算账本的起点
_SEEN_DEAL_COUNTS = {}
_LIVE_LAST = {"payload": None}

try:                         # QMT 环境内为内置注入;本地占位仅供阅读/测试
    passorder  # noqa: B018
except NameError:
    passorder = None
    get_trade_detail_data = None


# ══════════════════════ 账号:从运行环境直读 ══════════════════════
def _detect_account(C):
    """从 QMT 模型绑定关系读取资金账号/账号类型(多版本兼容)。"""
    g = globals()
    acc_id = str(g.get("account") or "")
    acc_type = str(g.get("accountType") or "")
    if not acc_id:
        for attr in ("accID", "accid", "accountid", "account_id"):
            v = getattr(C, attr, None)
            if v:
                acc_id = str(v)
                break
    if not acc_type:
        acc_type = "STOCK"
        for attr in ("accountType", "account_type", "accType"):
            v = getattr(C, attr, None)
            if v:
                acc_type = str(v)
                break
    if not acc_id:
        print("[sinan] 警告: 未能从运行环境读到资金账号——请确认模型已在"
              "QMT 交易面板绑定账号;临时兜底可手工设 _ACCOUNT['id']")
    return acc_id, acc_type


def _trade_mode(C):
    if TRADE_MODE in ("sim", "real"):
        return TRADE_MODE
    try:
        if getattr(C, "do_back_test", False):
            return "backtest"
        mode = getattr(C, "trade_mode", None)
        if mode is not None:
            return {0: "real", 1: "sim", 2: "sim"}.get(int(mode), "unknown")
    except Exception:
        pass
    return "unknown"


def _snapshot():
    accs = get_trade_detail_data(_ACCOUNT["id"], _ACCOUNT["type"], "account")
    cash = accs[0].m_dAvailable if accs else 0.0
    total = accs[0].m_dBalance if accs else 0.0
    pos = {}
    for p in get_trade_detail_data(_ACCOUNT["id"], _ACCOUNT["type"], "position"):
        pos[p.m_strInstrumentID] = [p.m_nVolume, p.m_nCanUseVolume, p.m_dLastPrice]
    return pos, cash, total


# ══════════════════════ targets / 备注 / 账本 ══════════════════════
def _checksum(targets):
    s = json.dumps(targets, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_today_targets(now):
    """当日全部策略的 targets(逐份校验,不合格的跳过并说明)。"""
    tdir = os.path.join(SHARE_DIR, "targets")
    ymd = now.strftime("%Y%m%d")
    out = []
    if not os.path.isdir(tdir):
        print("[sinan] targets 目录不存在:", tdir)
        return out
    for name in sorted(os.listdir(tdir)):
        if not (name.startswith("targets_") and name.endswith("_%s.json" % ymd)):
            continue
        path = os.path.join(tdir, name)
        try:
            with open(path, encoding="utf-8") as f:
                p = json.load(f)
            if p.get("date") != now.strftime("%Y-%m-%d"):
                raise ValueError("date 不符: %s" % p.get("date"))
            if _checksum(p.get("targets", {})) != p.get("checksum"):
                raise ValueError("checksum 不符")
            gen = datetime.fromisoformat(p["generated_at"])
            if (now - gen).total_seconds() > MAX_AGE_HOURS * 3600:
                raise ValueError("超出时效: %s" % p["generated_at"])
            if STRATEGIES and p.get("strategy") not in STRATEGIES:
                print("[sinan] 跳过 %s: 不在本壳 STRATEGIES 服务列表" % name)
                continue
            out.append(p)
        except Exception as e:                 # noqa: BLE001
            print("[sinan] 跳过 %s: %s" % (name, e))
    return out


def make_remark(strategy, ymd, seq):
    """下单备注「策略ID#日期#序号」;# 后可继续扩展字段。"""
    return "%s#%s#%d" % (strategy, ymd, seq)


def parse_remark(remark):
    """备注 → (策略ID, 扩展字段列表);非司南格式返回 (None, [])。"""
    parts = str(remark or "").split("#")
    return (parts[0], parts[1:]) if len(parts) >= 2 and parts[0] else (None, [])


def _ledger_path(strategy):
    return os.path.join(SHARE_DIR, "state", "ledger_%s.json" % strategy)


def load_ledger(strategy, capital):
    """策略虚拟账本;首次以 targets 的 capital 开账。"""
    path = _ledger_path(strategy)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"cash": float(capital), "pos": {}}


def save_ledger(strategy, led):
    path = _ledger_path(strategy)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=2)


def plan_orders(weights, ledger, prices, lot=LOT):
    """按策略自身账本算差额委托(纯函数,可本地测试)。

    卖单在前(先释放现金);返回 [(symbol, side, qty, price)];
    total = 账本现金 + 账本持仓市值(策略口径,与账户其他策略无关)。
    """
    total = ledger["cash"] + sum(q * prices.get(s, 0.0)
                                 for s, q in ledger["pos"].items())
    orders = []
    for sym in sorted(set(weights) | set(ledger["pos"])):
        px = prices.get(sym, 0.0)
        if px <= 0:
            continue
        target_qty = int(weights.get(sym, 0.0) * total / px / lot) * lot
        diff = target_qty - int(ledger["pos"].get(sym, 0))
        if diff:
            orders.append((sym, "sell" if diff < 0 else "buy", abs(diff), px))
    orders.sort(key=lambda o: 0 if o[1] == "sell" else 1)
    return orders


def _qmt_code(sym):
    return sym + (".SH" if sym.startswith(("5", "6")) else ".SZ")


# ══════════════════════ 调仓与回写 ══════════════════════
def do_rebalance(C):
    now = datetime.now()
    ymd = now.strftime("%Y%m%d")
    payloads = _load_today_targets(now)
    if not payloads:
        return
    acct_pos, _, _ = _snapshot()
    total_w = sum(sum((p.get("targets") or {}).values()) for p in payloads)
    if total_w > 1.0 + 1e-6:
        print("[sinan] 警告: 各策略目标权重合计 %.1f%% > 100%%,"
              "请检查各策略 capital/权重配置" % (total_w * 100))

    _MORNING_LEDGERS.clear()
    for p in payloads:
        strategy = p["strategy"]
        algo = dict(ALGO_DEFAULT)
        algo.update((p.get("qmt") or {}).get("algo") or {})
        pr_type = _PR_TYPE.get(str(algo["quote_mode"]), 5)
        offset = float(algo["price_offset"])
        max_qty = int(algo["max_order_qty"])

        led = load_ledger(strategy, p.get("capital", 0.0))
        _MORNING_LEDGERS[strategy] = json.loads(json.dumps(led))   # 深拷贝留底
        prices = {}
        for sym in set(p.get("targets") or {}) | set(led["pos"]):
            held = acct_pos.get(sym)
            px = held[2] if held else 0.0
            if px <= 0:
                tick = C.get_full_tick([_qmt_code(sym)])
                px = tick.get(_qmt_code(sym), {}).get("lastPrice", 0.0)
            prices[sym] = px

        placed = []
        seq = 0
        for sym, side, qty, px in plan_orders(p.get("targets") or {}, led, prices):
            remaining = qty
            while remaining > 0:
                lot_qty = min(remaining, max_qty)
                seq += 1
                remark = make_remark(strategy, ymd, seq)
                op = 23 if side == "buy" else 24
                order_px = px * (1 + offset) if side == "buy" else px * (1 - offset)
                passorder(op, 1101, _ACCOUNT["id"], _qmt_code(sym), pr_type,
                          round(order_px, 3) if pr_type == 11 else -1, lot_qty,
                          "sinan", 2, remark, C)
                placed.append({"symbol": sym, "side": side, "qty": lot_qty,
                               "price": px, "remark": remark})
                remaining -= lot_qty
            sign = 1 if side == "buy" else -1
            led["pos"][sym] = int(led["pos"].get(sym, 0)) + sign * qty
            led["cash"] -= sign * qty * px
            if led["pos"][sym] == 0:
                led["pos"].pop(sym)
        save_ledger(strategy, led)
        _write_fills(C, now, strategy, led, prices, placed)
        print("[sinan] %s: %d 笔委托" % (strategy, len(placed)))


def _collect_deals(ymd):
    """收集当日成交并按司南备注归因。"""
    deals_by_strategy = {}
    for d in get_trade_detail_data(_ACCOUNT["id"], _ACCOUNT["type"], "deal") or []:
        remark = getattr(d, "m_strRemark", "") or getattr(d, "m_strUserOrderId", "")
        strategy, ext = parse_remark(remark)
        if strategy is None or (ext and ext[0] != ymd):
            continue
        deals_by_strategy.setdefault(strategy, []).append({
            "symbol": str(getattr(d, "m_strInstrumentID", "")),
            "side": "buy" if int(getattr(d, "m_nOffsetFlag", 48)) in (48, 0) else "sell",
            "qty": int(getattr(d, "m_nVolume", 0)),
            "price": float(getattr(d, "m_dPrice", 0.0)),
            "remark": str(remark)})
    return deals_by_strategy


def _reconcile_strategy(C, now, strategy, deals, tag):
    """从调仓前账本按实际成交价幂等重演并回写 fills。"""
    led = _MORNING_LEDGERS.get(strategy)
    if led is None:
        print("[sinan] %s 无早间账本留底,fills 仅更新成交列表" % strategy)
        led = load_ledger(strategy, 0.0)
    else:
        led = json.loads(json.dumps(led))
        for t in deals:
            sign = 1 if t["side"] == "buy" else -1
            led["pos"][t["symbol"]] = (int(led["pos"].get(t["symbol"], 0))
                                       + sign * t["qty"])
            led["cash"] -= sign * t["qty"] * t["price"]
            if led["pos"].get(t["symbol"]) == 0:
                led["pos"].pop(t["symbol"])
        save_ledger(strategy, led)
    acct_pos, _, _ = _snapshot()
    prices = {s: acct_pos.get(s, [0, 0, 0.0])[2] for s in led["pos"]}
    _write_fills(C, now, strategy, led, prices, deals)
    print("[sinan] %s: fills 已按 %d 笔实际成交重算(%s)"
          % (strategy, len(deals), tag))


def do_snapshot(C):
    """15:05 收盘兜底,按当日实际成交修正 fills。"""
    now = datetime.now()
    for strategy, deals in _collect_deals(now.strftime("%Y%m%d")).items():
        _reconcile_strategy(C, now, strategy, deals, "收盘对账")
        _SEEN_DEAL_COUNTS[strategy] = len(deals)


def do_live_push(C):
    """每 5 秒推送账户快照;发现新成交时即时修正 fills。"""
    try:
        now = datetime.now()
        deals_by_strategy = _collect_deals(now.strftime("%Y%m%d"))
        for strategy, deals in deals_by_strategy.items():
            if len(deals) > _SEEN_DEAL_COUNTS.get(strategy, 0):
                _reconcile_strategy(C, now, strategy, deals, "盘中即时")
                _SEEN_DEAL_COUNTS[strategy] = len(deals)
        _write_live_state(C, now, deals_by_strategy)
    except Exception as e:                   # QMT 回调不可向外抛异常
        print("[live] 推送失败(下周期重试): %s: %s" % (type(e).__name__, e))


def _write_live_state(C, now, deals_by_strategy):
    """账户级快照原子写入 state/qmt_live.json,内容不变则不写盘。"""
    acct_pos, cash, total = _snapshot()
    strategies = {}
    sdir = os.path.join(SHARE_DIR, "state")
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if fn.startswith("ledger_") and fn.endswith(".json"):
                try:
                    with open(os.path.join(sdir, fn), encoding="utf-8") as f:
                        led = json.load(f)
                    strategies[fn[7:-5]] = {"cash": led.get("cash"),
                                            "pos": led.get("pos", {})}
                except Exception:
                    continue
    payload = {"account": _ACCOUNT["id"], "trade_mode": _trade_mode(C),
               "cash": cash, "total_asset": total,
               "positions": {s: {"qty": v[0], "avail_qty": v[1], "price": v[2]}
                             for s, v in acct_pos.items()},
               "deals_today": [dict(t, strategy=st)
                               for st, ds in deals_by_strategy.items() for t in ds],
               "strategies": strategies}
    if payload == _LIVE_LAST["payload"]:
        return
    _LIVE_LAST["payload"] = payload
    out = dict(payload)
    out["written_at"] = now.isoformat(timespec="seconds")
    os.makedirs(sdir, exist_ok=True)
    path, tmp = os.path.join(sdir, "qmt_live.json"), os.path.join(sdir, "qmt_live.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_fills(C, now, strategy, led, prices, orders):
    """按策略回写 fills(策略口径:账本现金/持仓;账户口径字段并列供对账)。"""
    pos_val = {s: q * prices.get(s, 0.0) for s, q in led["pos"].items()}
    total_s = led["cash"] + sum(pos_val.values())
    out = {"date": now.strftime("%Y-%m-%d"),
           "written_at": datetime.now().isoformat(timespec="seconds"),
           "strategy": strategy,
           "account": _ACCOUNT["id"],
           "trade_mode": _trade_mode(C),
           "total_asset": total_s, "cash": led["cash"],
           "weights": {s: round(v / total_s, 6)
                       for s, v in pos_val.items() if total_s > 0},
           "fills": orders,
           "positions": {s: {"qty": q, "avail_qty": q,
                             "price": prices.get(s, 0.0)}
                         for s, q in led["pos"].items()}}
    fdir = os.path.join(SHARE_DIR, "fills")
    os.makedirs(fdir, exist_ok=True)
    path = os.path.join(fdir, "fills_%s_%s.json" % (strategy,
                                                    now.strftime("%Y%m%d")))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


# ══════════════════════ RPC 转发(与 qmt_sdk 配套)══════════════════════
def to_jsonable(obj, _depth=0):
    if _depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    attrs = [a for a in dir(obj) if a.startswith("m_")]
    if attrs:
        return {a: to_jsonable(getattr(obj, a, None), _depth + 1) for a in attrs}
    try:
        return {k: to_jsonable(v, _depth + 1) for k, v in vars(obj).items()}
    except TypeError:
        return str(obj)


def dispatch(namespace, C, fn, args, kwargs, allow_trade=True):
    if not allow_trade and fn in _TRADE_FNS:
        raise PermissionError("只读通道(RPC_ALLOW_TRADE=False),拒绝: %s" % fn)
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
    if not token:
        return True
    return hmac.compare_digest(str(supplied or ""), token)


def ip_allowed(ip, allow_list):
    if not allow_list:
        return True
    addr = ipaddress.ip_address(ip)
    for entry in allow_list:
        try:
            if "/" in str(entry):
                if addr in ipaddress.ip_network(str(entry), strict=False):
                    return True
            elif addr == ipaddress.ip_address(str(entry)):
                return True
        except ValueError:
            continue
    return False


def _handle(conn, addr, namespace, C, token, allow_trade, allow_ips):
    if not ip_allowed(addr[0], allow_ips):
        print("[rpc] 拒绝非白名单来源:%r allow_ips=%r" % (addr[0], allow_ips))
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
                    result = dispatch(namespace, C, req["fn"], req.get("args"),
                                      req.get("kwargs"), allow_trade=allow_trade)
                    resp = {"id": rid, "ok": True, "result": result}
                except Exception as e:         # noqa: BLE001
                    resp = {"id": rid, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)}
                    traceback.print_exc()
                conn.sendall((json.dumps(resp, ensure_ascii=False,
                                         default=str) + "\n").encode("utf-8"))


def _make_server_socket():
    """Windows 独占端口,防止多个 QMT 模型静默争用同一 RPC 端口。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        # CPython/Windows 中常量值为 4;getattr 便于在非 Windows 上测试。
        srv.setsockopt(socket.SOL_SOCKET,
                       getattr(socket, "SO_EXCLUSIVEADDRUSE", 4), 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return srv


def serve(namespace, C, host=RPC_HOST, port=RPC_PORT, token=RPC_TOKEN,
          allow_trade=RPC_ALLOW_TRADE, allow_ips=None):
    allow_ips = list(allow_ips if allow_ips is not None else RPC_ALLOW_IPS)
    if host not in ("127.0.0.1", "localhost"):
        if not token:
            raise ValueError("非本机绑定(%s)必须配置非空 TOKEN" % host)
        if not allow_ips:
            raise ValueError("非本机绑定(%s)必须配置 ALLOW_IPS 白名单"
                             "(单 IP 或 CIDR,如 100.64.0.0/10)" % host)
    srv = _make_server_socket()
    srv.bind((host, port))
    srv.listen(4)

    def _loop():
        while True:
            try:
                conn, addr = srv.accept()
                print("[rpc] 收到连接:%r" % (addr,))
            except OSError:
                return
            threading.Thread(target=_handle,
                             args=(conn, addr, namespace, C, token,
                                   allow_trade, allow_ips), daemon=True).start()

    threading.Thread(target=_loop, daemon=True).start()
    print("[rpc] 运行脚本:%s" % globals().get("__file__", "<QMT编辑器>"))
    print("[rpc] 生效白名单:%r token_length=%d" % (allow_ips, len(token)))
    print("[rpc] 转发服务已启动 %s:%s(trade=%s)" % (host, port, allow_trade))
    return srv


# ══════════════════════ QMT 入口 ══════════════════════
def init(C):
    global _C
    _C = C
    _ACCOUNT["id"], _ACCOUNT["type"] = _detect_account(C)
    print("[sinan] 账号 %s(%s)/ 模式 %s"
          % (_ACCOUNT["id"] or "未识别", _ACCOUNT["type"], _trade_mode(C)))
    C.run_time("do_rebalance", "1nDay", "2026-01-01 14:45:00", "SH")
    C.run_time("do_snapshot", "1nDay", "2026-01-01 15:05:00", "SH")
    if LIVE_PUSH_ENABLE:
        C.run_time("do_live_push", LIVE_PUSH_PERIOD, "2026-01-01 09:30:00", "SH")
        print("[live] 实盘推送已启动(%s)→ %s"
              % (LIVE_PUSH_PERIOD,
                 os.path.join(SHARE_DIR, "state", "qmt_live.json")))
    if RPC_ENABLE:
        serve(globals(), C)


def handlebar(C):
    pass
