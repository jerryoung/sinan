# coding: utf-8
"""
QMT 薄壳策略模板(方案 §8.1)——放进标准版 QMT 的那约 100 行,不含任何策略逻辑。

职责只有四步(14:45 定时触发):
    读 targets_YYYYMMDD.json → 校验日期/校验和/时效(防止误用旧文件)
    → get_trade_detail_data 查当前持仓 → 按差额拆单 passorder
    → 回写 fills_YYYYMMDD.json(供本地 15:10 reconcile 对账)

本文件在本地环境不可执行(passorder / get_trade_detail_data / C 由 QMT
运行时注入),仅作部署模板;换券商/换通道时需要重写的也只有这一个文件。
校验和算法必须与 trend/live/targets.py 保持逐字节一致(两侧各持一份拷贝,
因为薄壳不 import 本地包——这正是文件桥接解耦的代价与全部代价)。
"""
import hashlib
import json
import os
from datetime import datetime

# ---- 部署时按实际环境修改的参数 -------------------------------------------
TARGETS_DIR = r"D:\trend\runtime\targets"   # 与本地系统共享的目录(同步盘)
STRATEGY_NAME = "combo_turtle_xsmom_x2"     # 本壳绑定的策略(一壳一策略)
FILLS_DIR = r"D:\trend\runtime\fills"
ACCOUNT = "8888888888"                      # 资金账号
ACCOUNT_TYPE = "STOCK"
MAX_AGE_HOURS = 8.0                         # targets 时效,与 settings.risk 一致
LOT = 100                                   # 股票/ETF 一手 100;可转债改 10
MAX_QTY_PER_ORDER = 100 * LOT               # 拆单:单笔委托数量上限
PR_TYPE = 5                                 # 报价方式: 5=最新价(按券商支持调整)

# QMT 环境内 passorder / get_trade_detail_data 为内置注入;本地占位仅供阅读
try:
    passorder  # noqa: B018
except NameError:
    passorder = None
    get_trade_detail_data = None


def init(C):
    """QMT 入口:注册每日 14:45 定时任务(晚于本地 14:35 run_signal 十分钟)。"""
    C.run_time("do_rebalance", "1nDay", "2026-01-01 14:45:00", "SH")


def _checksum(targets):
    """与 trend/live/targets.py::targets_checksum 同算法(键排序+紧凑分隔)。"""
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


def do_rebalance(C):
    now = datetime.now()
    payload = _load_targets(now)
    if payload is None:
        return
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
        while diff != 0:                     # 拆单:每笔不超过 MAX_QTY_PER_ORDER
            qty = min(abs(diff), MAX_QTY_PER_ORDER)
            op = 23 if diff > 0 else 24      # 23=买入, 24=卖出
            passorder(op, 1101, ACCOUNT, code, PR_TYPE, -1, qty,
                      "trend_shell", 2, "", C)
            orders.append({"symbol": sym, "side": "buy" if diff > 0 else "sell",
                           "qty": qty, "price": price})
            diff += qty if diff < 0 else -qty
    _write_fills(now, orders)


def _write_fills(now, orders):
    """回写成交回报。注:passorder 后成交需要时间,此处快照为委托后即时口径;
    更严谨的做法是在 15:05 再注册一次定时任务重写本文件。"""
    pos, cash, total = _snapshot()
    weights = {s: round(q * p / total, 6)
               for s, (q, a, p) in pos.items() if total > 0}
    out = {"date": now.strftime("%Y-%m-%d"),
           "written_at": datetime.now().isoformat(timespec="seconds"),
           "total_asset": total, "cash": cash,
           "weights": weights, "fills": orders,
           "positions": {s: {"qty": q, "avail_qty": a, "price": p}
                         for s, (q, a, p) in pos.items()}}
    path = os.path.join(FILLS_DIR, "fills_%s.json" % now.strftime("%Y%m%d"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[shell] fills 已回写:", path)
