# coding: utf-8
r"""
司南 ECS 统一脚本——放进大 QMT 的唯一一个模型,三件事全包:

    ① 14:45 执行当日全部策略的 targets(多策略共账号,下单备注归因)
    ② 15:05 按成交回报修正并回写各策略 fills(收盘兜底)
    ③ 常驻 socket 转发 QMT API(本地 qmt_sdk 同名调用;安全模型见下)
    ④ 每 5 秒推送实盘信息:新成交即时回写 fills,账户快照写
       state/qmt_live.json

── 服务器本地配置(第一性原理)──────────────────────────────────────
你在 QMT 把本模型绑定到 模拟/实盘 账号时已经选过 账号类型 与 资金账号,
脚本直接从运行环境读取(account/accountType 注入,ContextInfo 属性兜底),
不需要再填一遍。机器配置统一从 C:\sinan\config\qmt.json 读取;文件不存在
会自动生成关闭 RPC 的安全默认配置。以后整文件替换脚本无需再修改 Token、
白名单或共享目录,修改配置后重启策略生效。

── 多策略共账号:备注归因 + 策略虚拟账本 ─────────────────────────────
每笔新委托使用 <24 字符的确定性短备注，完整策略 ID/日期/序号保存在 execution
journal 并建立反向索引(旧版「策略ID#日期#序号」继续可读)。脚本为每个
策略维护一本虚拟账本(现金+持仓,SHARE_DIR/state/ledger_{策略}.json,
首次以 targets 的 capital 开账):下单差额 = 策略自己的目标 − 自己的账本,
互不读写对方持仓;15:05 查询当日成交(deal),按备注归回各策略、以实际
成交价重算账本与 fills。多个策略同持一只标的、甚至反向调仓都不会串账
——代价是极少数日子里可能左手换右手多付一次费用,换来的是归因精确。

── 安全(远程访问必读,详见 docs/qmt-deploy.md)─────────────────────
RPC 明文 TCP:传输安全靠 SSH 隧道/Tailscale;非 127.0.0.1 绑定强制
非空 TOKEN + 非空 IP 白名单(支持 CIDR),缺一拒绝启动;
rpc.allow_trade=false 时转发通道只读(targets 批处理执行不受影响)。

本文件在本地不可执行(passorder 等由 QMT 注入),部署=整文件粘贴;
targets 可由共享目录或 RPC v2 显式发布进入同一目录；校验和算法与
sinan/live/targets.py 逐字节一致。
"""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import queue
import re
import socket
import threading
import time
import traceback
from datetime import datetime

# ══════════════ 唯一配置入口(替换脚本时无需再修改)═══════════════
QMT_CONFIG_PATH = r"C:\sinan\config\qmt.json"
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

RPC_ENABLE = False           # 安全缺省；由 qmt.json 覆盖
RPC_HOST = "0.0.0.0"         # 远程监听须同时配置 Token 和 IP 白名单
RPC_PORT = 58620
RPC_TOKEN = ""               # 仅为安全缺省；实际值只放 qmt.json
RPC_ALLOW_TRADE = True       # 是否允许 passorder/cancel
RPC_ALLOW_IPS = []           # 单 IP 或 CIDR(100.64.0.0/10)

LIVE_PUSH_ENABLE = True
LIVE_PUSH_PERIOD = "5nSecond"

_TRADE_FNS = {"passorder", "cancel", "cancel_task"}
_RPC_GLOBAL_FNS = {
    "get_trade_detail_data", "get_last_order_id", "get_value_by_order_id",
    "timetag_to_datetime", "passorder", "cancel", "cancel_task",
}
_RPC_CONTEXT_FNS = {
    "get_full_tick", "get_stock_name", "get_market_data_ex",
    "get_trading_dates", "get_stock_list_in_sector",
    "get_instrument_detail",
}
_RPC_PROTOCOL = 2
_RPC_CAPABILITIES = ["qmt_api_queue", "publish_targets", "execution_status"]
_RPC_REQUEST_MAX_BYTES = 1024 * 1024
_RPC_QUEUE_SIZE = 64
_RPC_PUMP_LIMIT = 8
_RPC_CALL_TIMEOUT = 20.0
_RPC_API_QUEUE = queue.Queue(maxsize=_RPC_QUEUE_SIZE)
_PUBLISH_LOCK = threading.Lock()
_EXECUTION_LOCK = threading.RLock()
_C = None
_ACCOUNT = {"id": "", "type": "STOCK"}
_LIVE_LAST = {"payload": None}
_RPC_SERVER = None           # stop(ContextInfo) 负责关闭,释放模型后台监听


class QmtConfigError(ValueError):
    """QMT 本机配置不可用；消息不得包含 Token 内容。"""


def _default_local_config():
    """每次返回独立字典，避免测试或运行期间共享嵌套可变对象。"""
    return {
        "share_dir": r"C:\sinan\var\runtime",
        "rpc": {
            "enable": False,
            "host": "0.0.0.0",
            "port": 58620,
            "token": "",
            "allow_trade": True,
            "allow_ips": [],
        },
        "live_push": {"enable": True, "period": "5nSecond"},
    }


def _merge_config(defaults, supplied, prefix=""):
    """只合并约定字段；结构写错时在启动阶段直接指出。"""
    if not isinstance(supplied, dict):
        raise QmtConfigError("%s必须是对象" % (prefix or "配置根节点"))
    result = {}
    for key, default in defaults.items():
        field = "%s.%s" % (prefix, key) if prefix else key
        value = supplied.get(key, default)
        if isinstance(default, dict):
            result[key] = _merge_config(default, value, field)
        else:
            result[key] = value
    unknown = sorted(set(supplied) - set(defaults))
    if unknown:
        field = prefix or "配置根节点"
        raise QmtConfigError("%s包含未知字段:%s" % (field, ",".join(unknown)))
    return result


def _require_bool(value, field):
    if not isinstance(value, bool):
        raise QmtConfigError("%s 必须是 true/false" % field)


def _validate_local_config(cfg):
    if not isinstance(cfg["share_dir"], str) or not cfg["share_dir"].strip():
        raise QmtConfigError("share_dir 必须是非空字符串")
    cfg["share_dir"] = cfg["share_dir"].strip()

    rpc = cfg["rpc"]
    _require_bool(rpc["enable"], "rpc.enable")
    _require_bool(rpc["allow_trade"], "rpc.allow_trade")
    if not isinstance(rpc["host"], str) or not rpc["host"].strip():
        raise QmtConfigError("rpc.host 必须是非空字符串")
    rpc["host"] = rpc["host"].strip()
    if isinstance(rpc["port"], bool) or not isinstance(rpc["port"], int):
        raise QmtConfigError("rpc.port 必须是整数")
    if not 1 <= rpc["port"] <= 65535:
        raise QmtConfigError("rpc.port 必须在 1..65535")
    if not isinstance(rpc["token"], str):
        raise QmtConfigError("rpc.token 必须是字符串")
    rpc["token"] = rpc["token"].strip()
    if not isinstance(rpc["allow_ips"], list):
        raise QmtConfigError("rpc.allow_ips 必须是数组")
    clean_ips = []
    for entry in rpc["allow_ips"]:
        if not isinstance(entry, str) or not entry.strip():
            raise QmtConfigError("rpc.allow_ips 必须只包含 IP 或 CIDR 字符串")
        entry = entry.strip()
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise QmtConfigError("rpc.allow_ips 包含无效 IP/CIDR")
        clean_ips.append(entry)
    rpc["allow_ips"] = clean_ips

    live = cfg["live_push"]
    _require_bool(live["enable"], "live_push.enable")
    if not isinstance(live["period"], str) or not live["period"].strip():
        raise QmtConfigError("live_push.period 必须是非空字符串")
    live["period"] = live["period"].strip()
    if not re.match(r"^[1-9][0-9]*n(Second|Minute|Hour|Day)$",
                    live["period"]):
        raise QmtConfigError(
            "live_push.period 格式错误(例如 5nSecond、1nMinute)")

    if rpc["enable"] and rpc["host"] not in ("127.0.0.1", "localhost"):
        if len(rpc["token"]) < 32:
            raise QmtConfigError("远程 RPC 的 rpc.token 必须至少32位")
        if not rpc["allow_ips"]:
            raise QmtConfigError("远程 RPC 的 rpc.allow_ips 不能为空")
    return cfg


def load_local_config(path=QMT_CONFIG_PATH):
    """读取服务器本地配置；缺失时生成关闭 RPC 的安全默认文件。"""
    defaults = _default_local_config()
    if not os.path.exists(path):
        parent = os.path.dirname(path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(defaults, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except (OSError, TypeError) as e:
            raise QmtConfigError("无法创建 QMT 配置文件 %s:%s" % (path, e))
        print("[config] 已生成安全默认配置:%s" % path)
        print("[config] 请填写 Token、白名单并启用 RPC 后重启策略")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            supplied = json.load(f)
    except (OSError, ValueError) as e:
        raise QmtConfigError("无法读取 QMT 配置文件 %s:%s" %
                             (path, type(e).__name__))
    return _validate_local_config(_merge_config(defaults, supplied))


def _apply_local_config(cfg):
    """把启动快照接入现有薄壳变量；调用方无需了解配置文件结构。"""
    global SHARE_DIR, RPC_ENABLE, RPC_HOST, RPC_PORT, RPC_TOKEN
    global RPC_ALLOW_TRADE, RPC_ALLOW_IPS
    global LIVE_PUSH_ENABLE, LIVE_PUSH_PERIOD
    SHARE_DIR = cfg["share_dir"]
    rpc = cfg["rpc"]
    RPC_ENABLE = rpc["enable"]
    RPC_HOST = rpc["host"]
    RPC_PORT = rpc["port"]
    RPC_TOKEN = rpc["token"]
    RPC_ALLOW_TRADE = rpc["allow_trade"]
    RPC_ALLOW_IPS = list(rpc["allow_ips"])
    live = cfg["live_push"]
    LIVE_PUSH_ENABLE = live["enable"]
    LIVE_PUSH_PERIOD = live["period"]

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


def _parse_iso_datetime(value):
    """解析 targets 时间；兼容大 QMT 内置 Python 3.6。"""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    raise ValueError("ISO 时间格式错误: %s" % text)


def _safe_strategy(value):
    """把策略 ID 限制为单个安全文件名片段，同时保留合法中文名称。"""
    strategy = str(value or "")
    if (not strategy or strategy != strategy.strip() or len(strategy) > 128 or
            strategy in (".", "..") or
            any(ord(ch) < 32 for ch in strategy) or
            any(ch in '\\/:*?"<>|' for ch in strategy)):
        raise ValueError("策略名称不安全: %r" % strategy)
    return strategy


def _validate_target_payload(payload):
    """校验远端发布的不可信 targets，并返回可安全落盘的深拷贝。"""
    if not isinstance(payload, dict):
        raise ValueError("targets payload 必须是对象")
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ValueError("targets payload 无法序列化: %s" % e)
    if len(raw.encode("utf-8")) > _RPC_REQUEST_MAX_BYTES:
        raise ValueError("targets payload 过大")
    clean = json.loads(raw)
    clean["strategy"] = _safe_strategy(clean.get("strategy"))
    day = clean.get("date")
    if not isinstance(day, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError("targets 日期必须为 YYYY-MM-DD")
    try:
        parsed_day = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("targets 日期无效: %s" % day)
    if parsed_day.strftime("%Y-%m-%d") != day:
        raise ValueError("targets 日期无效: %s" % day)
    targets = clean.get("targets")
    if not isinstance(targets, dict):
        raise ValueError("targets 必须是对象")
    for symbol, weight in targets.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("targets 标的代码必须是非空字符串")
        if (isinstance(weight, bool) or not isinstance(weight, (int, float)) or
                not math.isfinite(float(weight))):
            raise ValueError("targets 权重必须是有限数字: %s" % symbol)
        if float(weight) < 0.0 or float(weight) > 1.0:
            raise ValueError("targets 权重必须在 0..1: %s" % symbol)
    if sum(float(weight) for weight in targets.values()) > 1.0 + 1e-8:
        raise ValueError("targets 总权重不能超过 1")
    if _checksum(targets) != clean.get("checksum"):
        raise ValueError("targets checksum 不符")
    return clean


def _atomic_write_json(path, payload):
    """同目录临时文件 + replace，避免 QMT 在半写状态读取 targets。"""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _publish_targets(payload, now=None):
    """接收一份显式发布的 targets；同 checksum 重复请求不重复写入。"""
    clean = _validate_target_payload(payload)
    strategy = clean["strategy"]
    day = clean["date"]
    checksum = clean["checksum"]
    filename = "targets_%s_%s.json" % (strategy, day.replace("-", ""))
    path = os.path.join(SHARE_DIR, "targets", filename)
    status = "accepted"
    with _PUBLISH_LOCK:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
            except (OSError, ValueError) as e:
                raise ValueError("已有 targets 无法读取: %s" % e)
            old = _validate_target_payload(old)
            if old["strategy"] != strategy or old["date"] != day:
                raise ValueError("已有 targets 身份字段不符")
            if old.get("checksum") == checksum:
                status = "duplicate"
            else:
                with _EXECUTION_LOCK:
                    execution = load_execution(strategy, day)
                    if execution and execution.get("status") not in (
                            "received", "planned"):
                        raise ValueError(
                            "执行已开始，拒绝替换不同 checksum 的 targets")
                    if execution:
                        os.remove(_execution_path(strategy, day))
                    status = "replaced"
                    _atomic_write_json(path, clean)
        else:
            _atomic_write_json(path, clean)
    return {"status": status, "strategy": strategy, "date": day,
            "checksum": checksum, "filename": filename}


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
            p = _validate_target_payload(p)
            if p.get("date") != now.strftime("%Y-%m-%d"):
                raise ValueError("date 不符: %s" % p.get("date"))
            expected = "targets_%s_%s.json" % (p["strategy"], ymd)
            if name != expected:
                raise ValueError("文件名与 targets 身份不符")
            gen = _parse_iso_datetime(p["generated_at"])
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
    """生成 QMT 要求的 <24 字符确定性备注；完整策略身份留在执行日志。"""
    digest = hashlib.sha256(
        (str(strategy) + "#" + str(ymd)).encode("utf-8")).hexdigest()[:10]
    number = int(seq)
    if number < 0:
        raise ValueError("委托序号不能为负数")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    encoded = "0"
    if number:
        chars = []
        while number:
            number, remainder = divmod(number, 36)
            chars.append(alphabet[remainder])
        encoded = "".join(reversed(chars))
    remark = "sn#%s#%s" % (digest, encoded)
    if len(remark) >= 24:
        raise ValueError("QMT 投资备注超过 23 字符")
    return remark


def parse_remark(remark):
    """解析旧版「策略ID#日期#序号」；新版短备注由 execution journal 反查。"""
    parts = str(remark or "").split("#")
    if parts and parts[0] == "sn":
        return None, []
    return (parts[0], parts[1:]) if len(parts) >= 2 and parts[0] else (None, [])


def _execution_remark_index(ymd):
    """建立当日短备注→策略索引；发现碰撞时拒绝归因而非猜测。"""
    try:
        day = datetime.strptime(str(ymd), "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("备注索引日期无效:%s" % ymd)
    index = {}
    for execution in _load_day_executions(day):
        strategy = execution.get("strategy")
        for order in execution.get("orders") or []:
            remark = str(order.get("remark") or "")
            if not remark:
                continue
            previous = index.get(remark)
            if previous is not None and previous != strategy:
                raise ValueError("执行日志备注碰撞:%s" % remark)
            index[remark] = strategy
    return index


def _strategy_from_remark(remark, ymd, index):
    if remark in index:
        return index[remark]
    strategy, ext = parse_remark(remark)
    if strategy is not None and ext and ext[0] == ymd:
        return strategy
    return None


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
    _atomic_write_json(path, led)


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


def _execution_path(strategy, day):
    strategy = _safe_strategy(strategy)
    if not isinstance(day, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError("执行日期必须为 YYYY-MM-DD")
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("执行日期无效: %s" % day)
    return os.path.join(
        SHARE_DIR, "executions",
        "execution_%s_%s.json" % (strategy, parsed.strftime("%Y%m%d")))


def load_execution(strategy, day):
    """读取指定策略/日期执行日志；不存在返回 None。"""
    path = _execution_path(strategy, day)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if value.get("strategy") != strategy or value.get("date") != day:
        raise ValueError("执行日志身份字段不符")
    return value


def save_execution(execution):
    """原子保存执行状态；每个 passorder 副作用前后都必须调用。"""
    execution["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = _execution_path(execution.get("strategy"), execution.get("date"))
    with _EXECUTION_LOCK:
        _atomic_write_json(path, execution)
    return path


def prepare_execution(payload, prices, now=None):
    """以持久化 baseline 生成确定性委托计划；同 checksum 只恢复不重算。"""
    clean = _validate_target_payload(payload)
    strategy, day = clean["strategy"], clean["date"]
    with _EXECUTION_LOCK:
        existing = load_execution(strategy, day)
        if existing is not None:
            if existing.get("checksum") != clean["checksum"]:
                raise ValueError("执行日志与 targets checksum 冲突")
            return existing

        timestamp = (now or datetime.now()).isoformat(timespec="seconds")
        baseline = json.loads(json.dumps(
            load_ledger(strategy, clean.get("capital", 0.0))))
        algo = dict(ALGO_DEFAULT)
        algo.update((clean.get("qmt") or {}).get("algo") or {})
        quote_mode = str(algo.get("quote_mode", "latest"))
        pr_type = _PR_TYPE.get(quote_mode, 5)
        offset = float(algo.get("price_offset", 0.0))
        max_qty = int(algo.get("max_order_qty", ALGO_DEFAULT["max_order_qty"]))
        if max_qty <= 0:
            raise ValueError("qmt.algo.max_order_qty 必须大于 0")

        orders = []
        seq = 0
        for sym, side, qty, px in plan_orders(
                clean.get("targets") or {}, baseline, prices):
            remaining = qty
            while remaining > 0:
                lot_qty = min(remaining, max_qty)
                seq += 1
                order_px = (px * (1 + offset) if side == "buy"
                            else px * (1 - offset))
                orders.append({
                    "sequence": seq,
                    "remark": make_remark(strategy, day.replace("-", ""), seq),
                    "symbol": sym,
                    "qmt_code": _qmt_code(sym),
                    "side": side,
                    "qty": lot_qty,
                    "reference_price": px,
                    "op": 23 if side == "buy" else 24,
                    "price_type": pr_type,
                    "order_price": round(order_px, 3) if pr_type == 11 else -1,
                    "status": "planned",
                })
                remaining -= lot_qty
        execution = {
            "strategy": strategy,
            "date": day,
            "checksum": clean["checksum"],
            "targets": clean.get("targets") or {},
            "status": "planned",
            "created_at": timestamp,
            "updated_at": timestamp,
            "baseline": baseline,
            "prices": {str(k): float(v) for k, v in prices.items()},
            "orders": orders,
        }
        save_execution(execution)
        return execution


def submit_execution(C, execution):
    """按日志逐笔最多提交一次；不确定窗口绝不自动重报。"""
    for order in execution.get("orders") or []:
        with _EXECUTION_LOCK:
            status = order.get("status", "planned")
            if status == "submitting":
                order["status"] = "uncertain"
                order["error"] = "模型重启时委托处于 submitting，无法证明是否已报单"
                execution["status"] = "uncertain"
                save_execution(execution)
                return execution
            if status == "uncertain":
                execution["status"] = "uncertain"
                return execution
            if status != "planned":
                continue

            order["status"] = "submitting"
            order["submitting_at"] = datetime.now().isoformat(timespec="seconds")
            execution["status"] = "submitting"
            save_execution(execution)
        try:
            result = passorder(
                order["op"], 1101, _ACCOUNT["id"], order["qmt_code"],
                order["price_type"], order["order_price"], order["qty"],
                "sinan", 2, order["remark"], C)
        except Exception as e:                 # noqa: BLE001
            with _EXECUTION_LOCK:
                order["status"] = "uncertain"
                order["error"] = "%s: %s" % (type(e).__name__, e)
                execution["status"] = "uncertain"
                save_execution(execution)
            print("[sinan] %s 委托状态不确定:%s"
                  % (order["remark"], order["error"]))
            return execution
        with _EXECUTION_LOCK:
            order["status"] = "submitted"
            order["submitted_at"] = datetime.now().isoformat(timespec="seconds")
            if result is not None:
                order["submit_result"] = to_jsonable(result)
            execution["status"] = "submitted"
            save_execution(execution)

    if not execution.get("orders"):
        execution["status"] = "submitted"
        save_execution(execution)
    elif all(o.get("status") != "planned" for o in execution["orders"]):
        execution["status"] = ("uncertain" if any(
            o.get("status") == "uncertain" for o in execution["orders"])
            else "submitted")
        save_execution(execution)
    return execution


# ══════════════════════ 调仓与回写 ══════════════════════
def do_rebalance(C):
    now = datetime.now()
    payloads = _load_today_targets(now)
    if not payloads:
        return
    acct_pos, _, _ = _snapshot()
    total_w = sum(sum((p.get("targets") or {}).values()) for p in payloads)
    if total_w > 1.0 + 1e-6:
        print("[sinan] 警告: 各策略目标权重合计 %.1f%% > 100%%,"
              "请检查各策略 capital/权重配置" % (total_w * 100))

    for p in payloads:
        strategy = p["strategy"]
        try:
            led = load_ledger(strategy, p.get("capital", 0.0))
            prices = {}
            for sym in set(p.get("targets") or {}) | set(led["pos"]):
                held = acct_pos.get(sym)
                px = held[2] if held else 0.0
                if px <= 0:
                    tick = C.get_full_tick([_qmt_code(sym)])
                    px = tick.get(_qmt_code(sym), {}).get("lastPrice", 0.0)
                prices[sym] = px
            execution = prepare_execution(p, prices, now=now)
            execution = submit_execution(C, execution)
            print("[sinan] %s: %d 笔委托，执行状态 %s"
                  % (strategy, len(execution.get("orders") or []),
                     execution.get("status")))
        except Exception as e:                 # 单策略失败不阻断其余策略
            print("[sinan] %s 调仓失败:%s: %s"
                  % (strategy, type(e).__name__, e))


def _read_qmt_attr(obj, names, default=None, required=False):
    """读取 QMT C++ 对象字段；必需字段不可转出时明确失败。"""
    errors = []
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception as e:                # C++ shared_ptr 转换可能在 getattr 抛错
            errors.append("%s:%s" % (name, type(e).__name__))
            continue
        if value is not None and value != "":
            return value
    if required:
        detail = ",".join(errors) if errors else "字段缺失"
        raise ValueError("QMT 必需字段 %s 不可读(%s)" % (names[0], detail))
    return default


def _remark_identity(obj):
    remark = _read_qmt_attr(
        obj, ("m_strRemark", "m_strUserOrderId", "m_strUserOrderID"), "")
    return str(remark or "")


def _normalize_symbol(value):
    return str(value or "").split(".", 1)[0]


def _collect_orders(ymd):
    """查询当日司南委托并按策略归因；匹配记录缺必需字段时拒绝伪成功。"""
    by_strategy = {}
    remark_index = _execution_remark_index(ymd)
    rows = get_trade_detail_data(
        _ACCOUNT["id"], _ACCOUNT["type"], "order") or []
    for obj in rows:
        remark = _remark_identity(obj)
        strategy = _strategy_from_remark(remark, ymd, remark_index)
        if strategy is None:
            continue
        try:
            symbol = _normalize_symbol(_read_qmt_attr(
                obj, ("m_strInstrumentID",), required=True))
            value = {
                "remark": remark,
                "symbol": symbol,
                "order_sys_id": str(_read_qmt_attr(
                    obj, ("m_strOrderSysID", "m_strOrderID"), "")),
                "status_raw": int(_read_qmt_attr(
                    obj, ("m_nOrderStatus",), required=True)),
                "qty": int(_read_qmt_attr(
                    obj, ("m_nVolumeTotalOriginal", "m_nOrderVolume",
                          "m_nVolume"), required=True)),
                "traded_qty": int(_read_qmt_attr(
                    obj, ("m_nVolumeTraded", "m_nTradedVolume",
                          "m_nDealVolume"), 0)),
                "cancel_qty": int(_read_qmt_attr(
                    obj, ("m_dCancelAmount", "m_nCancelVolume",
                          "m_nCanceledVolume"), 0)),
                "price": float(_read_qmt_attr(
                    obj, ("m_dLimitPrice", "m_dOrderPrice", "m_dPrice"),
                    0.0)),
            }
        except Exception as e:                 # 只污染所属策略，不毁掉整批查询
            value = {"remark": remark,
                     "_error": "%s: %s" % (type(e).__name__, e)}
        by_strategy.setdefault(strategy, []).append(value)
    return by_strategy


def _collect_deals(ymd):
    """查询当日司南真实成交并按策略归因。"""
    by_strategy = {}
    remark_index = _execution_remark_index(ymd)
    rows = get_trade_detail_data(
        _ACCOUNT["id"], _ACCOUNT["type"], "deal") or []
    for obj in rows:
        remark = _remark_identity(obj)
        strategy = _strategy_from_remark(remark, ymd, remark_index)
        if strategy is None:
            continue
        try:
            raw_time = _read_qmt_attr(
                obj, ("m_strTradeTime", "m_nTradeTime", "m_nTime"), "")
            convert = globals().get("timetag_to_datetime")
            try:
                trade_time = (convert(raw_time, "%Y-%m-%d %H:%M:%S")
                              if convert and raw_time != "" and
                              not isinstance(raw_time, str)
                              else str(raw_time or ""))
            except Exception:
                trade_time = str(raw_time or "")
            side_raw = int(_read_qmt_attr(
                obj, ("m_nOffsetFlag", "m_nDirection"), required=True))
            value = {
                "trade_id": str(_read_qmt_attr(
                    obj, ("m_strTradeID", "m_strDealID"), "")),
                "order_sys_id": str(_read_qmt_attr(
                    obj, ("m_strOrderSysID", "m_strOrderID"), "")),
                "remark": remark,
                "symbol": _normalize_symbol(_read_qmt_attr(
                    obj, ("m_strInstrumentID",), required=True)),
                "side": "buy" if side_raw in (48, 0, 23) else "sell",
                "qty": int(_read_qmt_attr(
                    obj, ("m_nVolume", "m_nTradeVolume"), required=True)),
                "price": float(_read_qmt_attr(
                    obj, ("m_dPrice", "m_dTradePrice"), required=True)),
                "trade_time": str(trade_time),
            }
        except Exception as e:
            value = {"remark": remark,
                     "_error": "%s: %s" % (type(e).__name__, e)}
        by_strategy.setdefault(strategy, []).append(value)
    return by_strategy


def _deal_key(deal):
    if deal.get("trade_id"):
        return "id:%s" % deal["trade_id"]
    fields = ("order_sys_id", "remark", "symbol", "side", "qty", "price",
              "trade_time")
    return "fallback:" + json.dumps(
        [deal.get(k) for k in fields], ensure_ascii=False, separators=(",", ":"))


def _rebuild_ledger(baseline, deals):
    """每次从磁盘 baseline 重演去重后的真实成交，天然支持重复轮询/重启。"""
    ledger = json.loads(json.dumps(baseline))
    ledger["cash"] = float(ledger.get("cash", 0.0))
    ledger["pos"] = {str(k): int(v) for k, v in (ledger.get("pos") or {}).items()}
    unique, seen = [], set()
    for raw in deals or []:
        deal = dict(raw)
        key = _deal_key(deal)
        if key in seen:
            continue
        seen.add(key)
        symbol = _normalize_symbol(deal.get("symbol"))
        side = deal.get("side")
        qty, price = int(deal.get("qty", 0)), float(deal.get("price", 0.0))
        if not symbol or side not in ("buy", "sell") or qty <= 0 or price <= 0:
            raise ValueError("成交记录关键字段无效:%r" % deal)
        deal["symbol"] = symbol
        sign = 1 if side == "buy" else -1
        ledger["pos"][symbol] = int(ledger["pos"].get(symbol, 0)) + sign * qty
        ledger["cash"] -= sign * qty * price
        if ledger["pos"][symbol] == 0:
            ledger["pos"].pop(symbol)
        unique.append(deal)
    return ledger, unique


def _order_state(raw_status):
    raw = int(raw_status)
    if raw == 56:
        return "filled"
    if raw in (53, 54):
        return "canceled"
    if raw == 57:
        return "rejected"
    if raw in (52, 55):
        return "partially_filled"
    return "accepted"


def _execution_state(order_states):
    if not order_states:
        return "filled"
    if any(s == "unreadable" for s in order_states):
        return "unreadable"
    if any(s == "uncertain" for s in order_states):
        return "uncertain"
    if all(s == "filled" for s in order_states):
        return "filled"
    if any(s == "partially_filled" for s in order_states):
        return "partially_filled"
    if any(s == "filled" for s in order_states):
        return "partially_filled"
    if any(s == "rejected" for s in order_states):
        return "rejected"
    if any(s == "canceled" for s in order_states):
        return "canceled"
    if any(s == "accepted" for s in order_states):
        return "accepted"
    return "submitted"


def _reconcile_execution(execution, qmt_orders, qmt_deals):
    """纯计算：把委托状态与真实成交折叠进执行日志和策略账本。"""
    value = json.loads(json.dumps(execution))
    remarks = {o.get("remark") for o in value.get("orders") or []}
    orders_by_remark = {o.get("remark"): o for o in (qmt_orders or [])
                        if o.get("remark") in remarks}
    relevant_deals = [d for d in (qmt_deals or []) if d.get("remark") in remarks]
    broken_deals = {d.get("remark"): d.get("_error") for d in relevant_deals
                    if d.get("_error")}
    relevant_deals = [d for d in relevant_deals if not d.get("_error")]
    ledger, unique = _rebuild_ledger(value.get("baseline") or {}, relevant_deals)
    deals_by_remark = {}
    for deal in unique:
        deals_by_remark.setdefault(deal.get("remark"), []).append(deal)

    for planned in value.get("orders") or []:
        remark = planned.get("remark")
        observed = orders_by_remark.get(remark)
        order_deals = deals_by_remark.get(remark, [])
        traded_qty = sum(int(d.get("qty", 0)) for d in order_deals)
        if observed and observed.get("_error"):
            planned["status"] = "unreadable"
            planned["error"] = observed["_error"]
        elif observed:
            for key in ("order_sys_id", "status_raw", "traded_qty",
                        "cancel_qty", "price"):
                planned[key] = observed.get(key)
            planned["status"] = _order_state(observed.get("status_raw"))
        if traded_qty >= int(planned.get("qty", 0)) and traded_qty > 0:
            planned["status"] = "filled"
        elif traded_qty > 0:
            planned["status"] = "partially_filled"
        if remark in broken_deals:
            planned["status"] = "unreadable"
            planned["error"] = broken_deals[remark]
        planned["traded_qty"] = traded_qty
    value["deals"] = unique
    value["status"] = _execution_state(
        [o.get("status", "submitted") for o in value.get("orders") or []])
    value["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return {"execution": value, "ledger": ledger, "fills": unique}


def _refresh_execution(C, execution, qmt_orders, qmt_deals, prices=None, now=None):
    """持久化一次幂等对账：日志、账本和 fills 共同来自同一计算结果。"""
    result = _reconcile_execution(execution, qmt_orders, qmt_deals)
    value, ledger, fills = (result["execution"], result["ledger"], result["fills"])
    save_execution(value)
    save_ledger(value["strategy"], ledger)
    market_prices = dict(value.get("prices") or {})
    market_prices.update(prices or {})
    for deal in fills:
        market_prices[deal["symbol"]] = deal["price"]
    _write_fills(C, now or datetime.now(), value["strategy"], ledger,
                 market_prices, fills, execution_status=value["status"],
                 order_states=value.get("orders") or [], day=value["date"])
    return value


def _load_day_executions(day):
    directory = os.path.join(SHARE_DIR, "executions")
    suffix = "_%s.json" % day.replace("-", "")
    out = []
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.startswith("execution_") or not name.endswith(suffix):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                value = json.load(f)
            _safe_strategy(value.get("strategy"))
            if value.get("date") != day:
                raise ValueError("执行日志日期不符")
            expected = "execution_%s_%s.json" % (
                value["strategy"], day.replace("-", ""))
            if name != expected:
                raise ValueError("执行日志文件名与身份不符")
            out.append(value)
        except Exception as e:                 # 一份坏日志不阻断其他策略
            print("[sinan] 跳过执行日志 %s:%s: %s"
                  % (name, type(e).__name__, e))
    return out


def _refresh_day(C, now, tag):
    day, ymd = now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d")
    executions = _load_day_executions(day)
    if not executions:
        return {}, {}
    orders_by_strategy = _collect_orders(ymd)
    deals_by_strategy = _collect_deals(ymd)
    acct_pos, _, _ = _snapshot()
    prices = {s: values[2] for s, values in acct_pos.items()}
    for execution in executions:
        strategy = execution["strategy"]
        try:
            refreshed = _refresh_execution(
                C, execution, orders_by_strategy.get(strategy, []),
                deals_by_strategy.get(strategy, []), prices=prices, now=now)
            print("[sinan] %s: %d 笔真实成交，执行状态 %s(%s)"
                  % (strategy, len(refreshed.get("deals") or []),
                     refreshed.get("status"), tag))
        except Exception as e:                 # 单策略坏数据不得瘫痪其他策略
            print("[sinan] %s 对账失败:%s: %s"
                  % (strategy, type(e).__name__, e))
    return orders_by_strategy, deals_by_strategy


def do_snapshot(C):
    """15:05 收盘兜底；包括零成交执行，保证次日有 fills 可对账。"""
    _refresh_day(C, datetime.now(), "收盘对账")


def do_live_push(C):
    """每周期刷新所有当日执行并推送账户快照。"""
    try:
        now = datetime.now()
        _, deals_by_strategy = _refresh_day(C, now, "盘中即时")
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


def _write_fills(C, now, strategy, led, prices, fills,
                 execution_status=None, order_states=None, day=None):
    """按策略回写 fills(策略口径:账本现金/持仓;账户口径字段并列供对账)。"""
    pos_val = {s: q * prices.get(s, 0.0) for s, q in led["pos"].items()}
    total_s = led["cash"] + sum(pos_val.values())
    day = day or now.strftime("%Y-%m-%d")
    out = {"date": day,
           "written_at": datetime.now().isoformat(timespec="seconds"),
           "strategy": strategy,
           "account": _ACCOUNT["id"],
           "trade_mode": _trade_mode(C),
           "total_asset": total_s, "cash": led["cash"],
           "weights": {s: round(v / total_s, 6)
                       for s, v in pos_val.items() if total_s > 0},
           "fills": fills,
           "positions": {s: {"qty": q, "avail_qty": q,
                             "price": prices.get(s, 0.0)}
                         for s, q in led["pos"].items()}}
    if execution_status is not None:
        out["execution_status"] = execution_status
    if order_states is not None:
        out["orders"] = order_states
    fdir = os.path.join(SHARE_DIR, "fills")
    path = os.path.join(fdir, "fills_%s_%s.json" % (strategy,
                                                    day.replace("-", "")))
    _atomic_write_json(path, out)


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
        out = {}
        for attr in attrs:
            try:
                value = getattr(obj, attr)
                out[attr] = to_jsonable(value, _depth + 1)
            except Exception:
                # QMT 实盘对象含 m_xtTag 等不可转出的 C++ shared_ptr；
                # 单字段不可读不能连带毁掉整份委托/成交列表。
                continue
        return out
    try:
        return {k: to_jsonable(v, _depth + 1) for k, v in vars(obj).items()}
    except TypeError:
        return str(obj)


def _validate_rpc_method(fn):
    """只暴露司南确实使用的最小 QMT API 面，拒绝任意属性遍历。"""
    if fn == "rpc.health":
        return
    if fn.startswith("C."):
        if fn[2:] in _RPC_CONTEXT_FNS:
            return
    elif fn in _RPC_GLOBAL_FNS:
        return
    raise PermissionError("RPC 不允许调用: %s" % fn)


def _execute_qmt_call(namespace, C, fn, args, kwargs):
    """仅由 QMT 策略线程请求泵调用，后台 socket 线程不得直接进入 QMT API。"""
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


def _reset_rpc_queue_for_test():
    """隔离测试用例；生产启动也可借此丢弃上次模型遗留的内存请求。"""
    global _RPC_API_QUEUE
    _RPC_API_QUEUE = queue.Queue(maxsize=_RPC_QUEUE_SIZE)


def _submit_api_request(namespace, C, fn, args, kwargs, allow_trade=True,
                        timeout=_RPC_CALL_TIMEOUT):
    """socket 线程提交请求并等待 QMT 策略线程执行。"""
    if not allow_trade and fn in _TRADE_FNS:
        raise PermissionError("只读通道(RPC_ALLOW_TRADE=False),拒绝: %s" % fn)
    request = {
        "namespace": namespace,
        "context": C,
        "fn": fn,
        "args": args or [],
        "kwargs": kwargs or {},
        "event": threading.Event(),
        "result": None,
        "error": None,
        "deadline": time.monotonic() + float(timeout),
    }
    try:
        _RPC_API_QUEUE.put_nowait(request)
    except queue.Full:
        raise RuntimeError("QMT API 请求队列已满")
    if not request["event"].wait(float(timeout)):
        raise TimeoutError("QMT API 调用超时: %s" % fn)
    if request["error"] is not None:
        raise request["error"]
    return request["result"]


def do_rpc_pump(C):
    """QMT 策略线程定时回调：串行执行由 RPC 后台线程提交的 API 请求。"""
    for _ in range(_RPC_PUMP_LIMIT):
        try:
            request = _RPC_API_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            if time.monotonic() > request["deadline"]:
                raise TimeoutError("QMT API 请求等待执行已超时: %s"
                                   % request["fn"])
            request["result"] = _execute_qmt_call(
                request["namespace"], request.get("context") or C,
                request["fn"], request["args"], request["kwargs"])
        except Exception as e:                 # noqa: BLE001
            request["error"] = e
        finally:
            request["event"].set()
            _RPC_API_QUEUE.task_done()


def _execution_status_payload(strategy, day):
    """只按校验后的精确身份读取日志/fills，不接受任意服务端路径。"""
    strategy = _safe_strategy(strategy)
    if not isinstance(day, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError("执行日期必须为 YYYY-MM-DD")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("执行日期无效: %s" % day)
    journal = load_execution(strategy, day)
    fills_path = os.path.join(
        SHARE_DIR, "fills",
        "fills_%s_%s.json" % (strategy, day.replace("-", "")))
    fills = None
    if os.path.exists(fills_path):
        with open(fills_path, encoding="utf-8") as f:
            fills = json.load(f)
        if fills.get("strategy") != strategy or fills.get("date") != day:
            raise ValueError("fills 身份字段不符")
    return {"found": journal is not None or fills is not None,
            "journal": journal, "fills": fills}


def dispatch(namespace, C, fn, args, kwargs, allow_trade=True):
    if fn == "rpc.health":
        return {"service": "sinan-qmt-rpc", "protocol": _RPC_PROTOCOL,
                "capabilities": list(_RPC_CAPABILITIES),
                "account": _ACCOUNT["id"], "account_type": _ACCOUNT["type"],
                "trade_mode": _trade_mode(C), "allow_trade": bool(allow_trade),
                "server_time": datetime.now().isoformat(timespec="seconds")}
    if fn == "rpc.publish_targets":
        if not allow_trade:
            raise PermissionError(
                "只读通道(RPC_ALLOW_TRADE=False),拒绝: %s" % fn)
        if kwargs or not isinstance(args, list) or len(args) != 1:
            raise ValueError("rpc.publish_targets 只接受一个 payload 参数")
        return _publish_targets(args[0])
    if fn == "rpc.execution_status":
        if kwargs or not isinstance(args, list) or len(args) != 2:
            raise ValueError("rpc.execution_status 只接受 strategy, date")
        return _execution_status_payload(args[0], args[1])
    _validate_rpc_method(fn)
    return _submit_api_request(namespace, C, fn, args, kwargs,
                               allow_trade=allow_trade)


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
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if len(line) > _RPC_REQUEST_MAX_BYTES:
                    return
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
                try:
                    conn.sendall((json.dumps(resp, ensure_ascii=False,
                                             default=str) + "\n").encode("utf-8"))
                except OSError:
                    return
            if len(buf) > _RPC_REQUEST_MAX_BYTES:
                return


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
    global _C, _RPC_SERVER
    _C = C
    _RPC_SERVER = None
    try:
        cfg = load_local_config()
    except QmtConfigError as e:
        cfg = _default_local_config()
        print("[config] 配置不可用:%s" % e)
        print("[rpc] RPC 未启动:请修复 %s 后重启策略" % QMT_CONFIG_PATH)
    _apply_local_config(cfg)
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
        try:
            _RPC_SERVER = serve(
                globals(), C, host=RPC_HOST, port=RPC_PORT, token=RPC_TOKEN,
                allow_trade=RPC_ALLOW_TRADE, allow_ips=RPC_ALLOW_IPS)
            _reset_rpc_queue_for_test()
            C.run_time("do_rpc_pump", "1nSecond",
                       "2026-01-01 09:00:00", "SH")
        except (OSError, ValueError) as e:
            _RPC_SERVER = None
            print("[rpc] RPC 未启动:%s" % e)


def stop(C):
    """QMT 策略停止回调:关闭后台 RPC socket,保证模型可直接热重启。"""
    global _RPC_SERVER
    if _RPC_SERVER is not None:
        try:
            _RPC_SERVER.close()
            print("[rpc] 策略停止,监听端口已释放")
        except OSError as e:
            print("[rpc] 关闭监听时告警:%s" % e)
        finally:
            _RPC_SERVER = None


def handlebar(C):
    pass
