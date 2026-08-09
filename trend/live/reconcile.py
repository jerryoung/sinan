"""
对账(方案 §8.1 的 15:10 环节):fills 实际权重 vs targets 目标权重。

每日收盘后强制对账,偏差超阈值告警;账实不符次日暂停自动执行(§8.3)——
对账是文件桥接模式的安全网:targets 写出去之后发生的一切(部分成交、
涨跌停未成、薄壳没跑),都只能靠这一步发现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ReconcileReport:
    ok: bool
    deviations: list = field(default_factory=list)   # [{symbol,target,actual,diff}]


def reconcile(
    targets_payload: dict,
    fills: dict,
    *,
    tolerance: float = 0.01,
    notify_fn: Callable | None = None,
) -> ReconcileReport:
    """
    targets_payload: load_and_validate 返回的 payload(取其 targets 字段)
    fills:           {symbol: 实际权重}——薄壳回写 fills 文件里的 weights,
                     已统一为权重口径(qty×price/total_asset 由写入侧折算)
    对账遍历 目标 ∪ 实际 的并集:目标有而实际没有(没成交)、实际有而目标
    没有(残留持仓/误操作)都是账实不符,|target − actual| > tolerance
    记入 deviations;存在偏差且给了 notify_fn 则推送告警。
    """
    targets = targets_payload.get("targets", {}) or {}
    actual = {str(s): float(w) for s, w in (fills or {}).items()}
    devs: list[dict] = []
    for s in sorted(set(targets) | set(actual)):
        t = float(targets.get(s, 0.0))
        a = actual.get(s, 0.0)
        if abs(t - a) > tolerance:
            devs.append({"symbol": s, "target": round(t, 6),
                         "actual": round(a, 6), "diff": round(a - t, 6)})

    if devs and notify_fn is not None:
        lines = "; ".join(
            f"{d['symbol']} 目标{d['target']:.2%} 实际{d['actual']:.2%}"
            for d in devs)
        notify_fn(f"[对账告警] {targets_payload.get('date', '?')} "
                  f"偏差超阈值({tolerance:.2%}): {lines}")
    return ReconcileReport(ok=not devs, deviations=devs)
