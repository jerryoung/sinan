"""
Broker 抽象与三个通道适配器(方案 §8.2)。

监管无论怎么变,损失都只是一个适配器:
    QmtShellBroker  主通道 —— targets 文件桥接标准版 QMT 薄壳(§8.1)
    MiniQmtBroker   备通道 —— xtquant 直连 miniQMT(需券商权限,仅 Windows)
    ManualBroker    兜底   —— 目标仓位推送人工执行,本地 json 记账

策略与数据资产完全独立于任何券商终端,通道只是执行层的一个适配器。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import pandas as pd

from .notify import notify


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float          # 持有数量(股/份/张)
    avail_qty: float    # 可用数量(T+1 品种当日买入部分不可卖)
    price: float        # 最新价/成本价(通道口径,仅供权重估算)


@dataclass(frozen=True)
class OrderResult:
    symbol: str
    target_qty: float
    ok: bool
    msg: str


class Broker(Protocol):
    """执行通道协议:三个适配器结构性满足即可,无须继承。"""

    def get_positions(self) -> dict[str, Position]: ...
    def get_cash(self) -> float: ...
    def order_target(self, symbol: str, target_qty: int) -> OrderResult: ...


# --------------------------------------------------------------------------
# ManualBroker:兜底通道
# --------------------------------------------------------------------------
class ManualBroker:
    """
    人工执行兜底:账户状态维护在本地 json 文件里,order_target 只把目标
    追加进 pending 并经 notify 推送——真正下单由人完成,完成后调
    set_position / set_cash 手工回填。任何自动通道失效时都能退回这里。
    """

    def __init__(self, state_path: str | Path, *,
                 notify_fn: Callable | None = None) -> None:
        self.state_path = Path(state_path)
        self._notify = notify_fn or notify

    # ---- 状态文件读写 -------------------------------------------------
    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"cash": 0.0, "positions": {}, "pending": []}

    def _save(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 人工成交后手动回填 -------------------------------------------
    def set_cash(self, cash: float) -> None:
        st = self._load()
        st["cash"] = float(cash)
        self._save(st)

    def set_position(self, symbol: str, qty: float,
                     avail_qty: float | None = None, price: float = 0.0) -> None:
        st = self._load()
        st["positions"][str(symbol)] = {
            "qty": float(qty),
            "avail_qty": float(qty if avail_qty is None else avail_qty),
            "price": float(price),
        }
        self._save(st)

    # ---- Broker 协议 --------------------------------------------------
    def get_positions(self) -> dict[str, Position]:
        return {s: Position(symbol=s, qty=p["qty"], avail_qty=p["avail_qty"],
                            price=p["price"])
                for s, p in self._load()["positions"].items()}

    def get_cash(self) -> float:
        return float(self._load()["cash"])

    def order_target(self, symbol: str, target_qty: int) -> OrderResult:
        """只记录 + 推送,不改变持仓——账实由人工回填保证。"""
        st = self._load()
        st["pending"].append({
            "symbol": str(symbol),
            "target_qty": target_qty,
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        self._save(st)
        self._notify(f"[人工执行] {symbol} 目标数量 {target_qty},"
                     f"请手动下单后回填状态文件 {self.state_path.name}")
        return OrderResult(symbol=str(symbol), target_qty=target_qty,
                           ok=True, msg="已记录并推送人工执行")


# --------------------------------------------------------------------------
# QmtShellBroker:主通道(targets 文件桥接)
# --------------------------------------------------------------------------
class QmtShellBroker:
    """
    标准版 QMT 薄壳桥接(§8.1):本进程与 QMT 只通过两个 json 文件说话。

    order_target 不直接下单——差额计算与拆单发生在薄壳侧;本类只负责
    聚合目标(由 run_signal 统一经 targets.py 写 targets 文件)与解析
    薄壳回写的 fills_YYYYMMDD.json。持仓/资金查询同样来自最近一份 fills。
    """

    def __init__(self, targets_dir: str | Path, fills_dir: str | Path) -> None:
        self.targets_dir = Path(targets_dir)
        self.fills_dir = Path(fills_dir)
        self.pending: dict[str, float] = {}   # order_target 聚合的目标数量

    def order_target(self, symbol: str, target_qty: int) -> OrderResult:
        self.pending[str(symbol)] = target_qty
        return OrderResult(
            symbol=str(symbol), target_qty=target_qty, ok=True,
            msg="不直接下单: 目标经 targets 文件桥接,由 QMT 薄壳 14:45 执行")

    # ---- fills 解析 ---------------------------------------------------
    @staticmethod
    def read_fills(fills_dir: str | Path, date) -> dict:
        """解析薄壳回写的 fills_YYYYMMDD.json;文件缺失抛 FileNotFoundError。"""
        ymd = pd.Timestamp(date).strftime("%Y%m%d")
        fp = Path(fills_dir) / f"fills_{ymd}.json"
        if not fp.exists():
            raise FileNotFoundError(f"fills 文件不存在: {fp}")
        return json.loads(fp.read_text(encoding="utf-8"))

    @staticmethod
    def latest_fills(fills_dir: str | Path) -> dict | None:
        """最近一份 fills(文件名按日期排序);目录不存在或为空返回 None。"""
        d = Path(fills_dir)
        if not d.exists():
            return None
        files = sorted(d.glob("fills_*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))

    # ---- Broker 协议(基于最近一份 fills 的快照口径)-------------------
    def get_positions(self) -> dict[str, Position]:
        pay = self.latest_fills(self.fills_dir) or {}
        out: dict[str, Position] = {}
        for s, p in (pay.get("positions") or {}).items():
            qty = float(p.get("qty", 0.0))
            out[s] = Position(symbol=s, qty=qty,
                              avail_qty=float(p.get("avail_qty", qty)),
                              price=float(p.get("price", 0.0)))
        return out

    def get_cash(self) -> float:
        pay = self.latest_fills(self.fills_dir) or {}
        return float(pay.get("cash", 0.0))


# --------------------------------------------------------------------------
# MiniQmtBroker:备通道(xtquant 直连)
# --------------------------------------------------------------------------
class MiniQmtBroker:
    """
    miniQMT 直连备通道。xtquant 随 QMT 客户端分发、无法 pip 安装,
    本地开发环境构造即报清晰错误;方法体为实盘接入时的实现骨架。
    """

    def __init__(self, account_id: str, mini_qmt_path: str = "",
                 session_id: int = 0) -> None:
        try:
            from xtquant import xttrader, xttype  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "MiniQmtBroker 需在装有 miniQMT 的环境使用: xtquant 随 QMT "
                "客户端分发,无法 pip 安装;请在开通权限的 Windows 实盘机运行,"
                "或改用 QmtShellBroker / ManualBroker") from e
        self.account_id = account_id
        self.mini_qmt_path = mini_qmt_path
        self.session_id = session_id
        # 实盘接入骨架(按 xtquant 文档补全):
        #   self._trader = xttrader.XtQuantTrader(mini_qmt_path, session_id)
        #   self._trader.start(); self._trader.connect()
        #   self._account = xttype.StockAccount(account_id)
        #   self._trader.subscribe(self._account)
        self._trader = None
        self._account = None

    def get_positions(self) -> dict[str, Position]:
        # 骨架: for p in self._trader.query_stock_positions(self._account):
        #           out[p.stock_code[:6]] = Position(p.stock_code[:6],
        #               p.volume, p.can_use_volume, p.open_price)
        raise NotImplementedError("待实盘环境接入 xtquant 后补全")

    def get_cash(self) -> float:
        # 骨架: return self._trader.query_stock_asset(self._account).cash
        raise NotImplementedError("待实盘环境接入 xtquant 后补全")

    def order_target(self, symbol: str, target_qty: int) -> OrderResult:
        # 骨架: 查持仓算差额 → xtconstant.STOCK_BUY/SELL → order_stock(...)
        raise NotImplementedError("待实盘环境接入 xtquant 后补全")
