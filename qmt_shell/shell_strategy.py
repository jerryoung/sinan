# coding: utf-8
"""
QMT 薄壳策略模板 v2(方案 §8.1)——放进大 QMT(标准版内置 Python)的执行壳,
不含任何策略逻辑。模拟盘/实盘由你在 QMT 客户端把本模型绑定到 模拟/实盘 账号
时决定,壳只如实上报;信号与数据全部在 sinan 侧生成(回测–实盘一致性),
壳只消费 targets 文件并用 QMT 实时行情定价下单。

职责(每日两次定时触发):
    14:45 do_rebalance:
        读 targets_{策略}_{当日}.json → 校验 日期/策略/校验和/时效
        → 查账户持仓 → 按差额 passorder(报价方式/拆单来自 targets 的 qmt.algo)
        → 回写 fills_{策略}_{当日}.json(委托后即时快照)
    15:05 do_snapshot:
        成交落定后重写同一份 fills(收盘快照,当日最终口径)——
        sinan 的持仓/收益统计与次日信号都以这份为准。

trade_mode(模拟/实盘)识别:QMT 内置环境没有稳定公开的判别 API,壳按
    ① TRADE_MODE 常量(部署时人工声明,最可靠)
    ② 自动探测(尽力而为:回测标志/账号注册表),失败记 "unknown"
的顺序取值写入 fills;sinan 平台直接读取展示,不做二次猜测。

本文件在本地不可执行(passorder / get_trade_detail_data / C 由 QMT 运行时
注入),仅作部署模板;校验和算法必须与 sinan/live/targets.py 逐字节一致
(两侧各持一份拷贝——薄壳不 import 本地包,这是文件桥接解耦的全部代价)。
"""
import hashlib
import json
import os
from datetime import datetime

# ---- 部署时按实际环境修改的参数 -------------------------------------------
TARGETS_DIR = r"D:\sinan\var\runtime\targets"   # 与本地系统共享的目录(同步盘)
FILLS_DIR = r"D:\sinan\var\runtime\fills"
STRATEGY_NAME = "combo_turtle_xsmom_x2"     # 本壳绑定的策略(一壳一策略)
ACCOUNT = "8888888888"                      # 资金账号(可被 targets 的 qmt.account 覆盖)
ACCOUNT_TYPE = "STOCK"
TRADE_MODE = "auto"                         # "sim" 模拟盘 / "real" 实盘 / "auto" 尝试探测
MAX_AGE_HOURS = 8.0                         # targets 时效,与 settings.risk 一致
LOT = 100                                   # 股票/ETF 一手 100;可转债改 10

# 下单算法缺省值(targets 的 qmt.algo 同名键优先;每策略可配不同算法)
ALGO_DEFAULT = {
    "quote_mode": "latest",     # latest=最新价(prType 5)/ limit=限价(prType 11)
    "price_offset": 0.002,      # limit 模式:买价上浮/卖价下调的比例(提高成交率)
    "max_order_qty": 100 * LOT, # 拆单:单笔委托数量上限
}
_PR_TYPE = {"latest": 5, "limit": 11}       # 报价方式映射(按券商支持调整)

# QMT 环境内 passorder / get_trade_detail_data 为内置注入;本地占位仅供阅读
try:
    passorder  # noqa: B018
except NameError:
    passorder = None
    get_trade_detail_data = None


def init(C):
    """QMT 入口:14:45 调仓(晚于本地 14:35 run_signal),15:05 收盘快照回写。"""
    C.run_time("do_rebalance", "1nDay", "2026-01-01 14:45:00", "SH")
    C.run_time("do_snapshot", "1nDay", "2026-01-01 15:05:00", "SH")


# --------------------------------------------------------------------------
# targets 读取与校验
# --------------------------------------------------------------------------
def _checksum(targets):
    """与 sinan/live/targets.py::targets_checksum 同算法(键排序+紧凑分隔)。"""
    s = json.dumps(targets, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_targets(now):
    """读取并校验当日 targets;任何一步不通过都返回 None(拒绝执行,§8.3)。"""
    path = os.path.join(TARGETS_DIR, "targets_%s_%s.json"
                        % (STRATEGY_NAME, now.strftime("%Y%m%d")))
    if not os.path.exists(path):
        print("[shell] targets 不存在,跳过:", path)
        return None
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if payload.get("date") != now.strftime("%Y-%m-%d"):
        print("[shell] 日期不符,拒绝执行:", payload.get("date"))
        return None
    if payload.get("strategy") != STRATEGY_NAME:
        print("[shell] 策略不符,拒绝执行:", payload.get("strategy"))
        return None
    if _checksum(payload.get("targets", {})) != payload.get("checksum"):
        print("[shell] 校验和不符,拒绝执行")
        return None
    gen = datetime.fromisoformat(payload["generated_at"])
    if (now - gen).total_seconds() > MAX_AGE_HOURS * 3600:
        print("[shell] 超出时效,拒绝执行:", payload["generated_at"])
        return None
    return payload


# --------------------------------------------------------------------------
# 账户与模式
# --------------------------------------------------------------------------
def _trade_mode(C):
    """模拟/实盘识别:常量声明优先,auto 时尽力探测,失败 unknown。"""
    if TRADE_MODE in ("sim", "real"):
        return TRADE_MODE
    try:                                    # 尽力而为:各版本注入属性不稳定
        if getattr(C, "do_back_test", False):
            return "backtest"
        mode = getattr(C, "trade_mode", None)   # 部分版本有此属性(1/2=模拟)
        if mode is not None:
            return {0: "real", 1: "sim", 2: "sim"}.get(int(mode), "unknown")
    except Exception:
        pass
    return "unknown"


def _snapshot():
    """查询资金与持仓: ({symbol: [qty, avail, price]}, cash, total_asset)。"""
    accs = get_trade_detail_data(ACCOUNT, ACCOUNT_TYPE, "account")
    cash = accs[0].m_dAvailable if accs else 0.0
    total = accs[0].m_dBalance if accs else 0.0
    pos = {}
    for p in get_trade_detail_data(ACCOUNT, ACCOUNT_TYPE, "position"):
        pos[p.m_strInstrumentID] = [p.m_nVolume, p.m_nCanUseVolume, p.m_dLastPrice]
    return pos, cash, total


def _qmt_code(sym):
    """6 位代码 → QMT 带市场后缀代码(5/6 开头沪市,其余深市;按需扩展)。"""
    return sym + (".SH" if sym.startswith(("5", "6")) else ".SZ")


# --------------------------------------------------------------------------
# 调仓与回写
# --------------------------------------------------------------------------
def do_rebalance(C):
    global ACCOUNT
    now = datetime.now()
    payload = _load_targets(now)
    if payload is None:
        return
    qmt_cfg = payload.get("qmt") or {}
    if qmt_cfg.get("account"):              # 策略级账号绑定(多策略多账号)
        ACCOUNT = str(qmt_cfg["account"])
    algo = dict(ALGO_DEFAULT)
    algo.update(qmt_cfg.get("algo") or {})  # 策略级下单算法覆盖缺省
    pr_type = _PR_TYPE.get(str(algo["quote_mode"]), 5)
    offset = float(algo["price_offset"])
    max_qty = int(algo["max_order_qty"])

    pos, cash, total = _snapshot()
    orders = []
    for sym, w in sorted(payload["targets"].items()):
        code = _qmt_code(sym)
        held, avail, price = pos.get(sym, [0, 0, 0.0])
        if price <= 0:                       # 未持仓标的取最新 tick 价
            tick = C.get_full_tick([code])
            price = tick[code]["lastPrice"] if code in tick else 0.0
        if price <= 0:
            print("[shell] 无法取价,跳过:", sym)
            continue
        target_qty = int(w * total / price / LOT) * LOT   # 手数取整(向下)
        diff = target_qty - held
        if diff < 0:
            diff = -min(-diff, avail)        # T+1: 卖出不超过可用数量
        while diff != 0:                     # 拆单:每笔不超过 max_order_qty
            qty = min(abs(diff), max_qty)
            op = 23 if diff > 0 else 24      # 23=买入, 24=卖出
            # 限价模式:买单上浮 / 卖单下调 offset,提高成交率;最新价模式忽略价格
            px = price * (1 + offset) if diff > 0 else price * (1 - offset)
            passorder(op, 1101, ACCOUNT, code, pr_type,
                      round(px, 3) if pr_type == 11 else -1, qty,
                      "sinan_shell", 2, "", C)
            orders.append({"symbol": sym, "side": "buy" if diff > 0 else "sell",
                           "qty": qty, "price": price})
            diff += qty if diff < 0 else -qty
    _write_fills(C, now, orders)


def do_snapshot(C):
    """15:05 成交落定后重写当日 fills:收盘快照是当日最终口径。"""
    _write_fills(C, datetime.now(), None)


def _write_fills(C, now, orders):
    """回写成交回报;orders=None 表示快照重写(保留 14:45 已记录的委托)。"""
    path = os.path.join(FILLS_DIR, "fills_%s_%s.json"
                        % (STRATEGY_NAME, now.strftime("%Y%m%d")))
    if orders is None:                       # 快照重写:沿用当日已记录的委托
        orders = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                orders = json.load(f).get("fills", [])
    pos, cash, total = _snapshot()
    weights = {s: round(q * p / total, 6)
               for s, (q, a, p) in pos.items() if total > 0}
    out = {"date": now.strftime("%Y-%m-%d"),
           "written_at": datetime.now().isoformat(timespec="seconds"),
           "strategy": STRATEGY_NAME,
           "account": ACCOUNT,
           "trade_mode": _trade_mode(C),    # sim/real/backtest/unknown
           "total_asset": total, "cash": cash,
           "weights": weights, "fills": orders,
           "positions": {s: {"qty": q, "avail_qty": a, "price": p}
                         for s, (q, a, p) in pos.items()}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[shell] fills 已回写:", path)
