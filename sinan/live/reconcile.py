"""
对账:fills 实际权重 vs targets 目标权重。

对账是文件桥接模式的安全网:targets 写出去之后发生的一切(部分成交、
涨跌停未成、薄壳压根没跑),都只能靠这一步发现。

**接线点是次日出信号时**(scripts/run_signal.py):run_signal 本就要读最近
一份 fills 当作当前持仓,顺手把那一天的 targets 取出来对一次账——不需要
额外的 15:10 定时任务,且"上一次执行到底成没成"正是决定今天该不该照常
下单的信息。结果写进当日 targets payload 的 reconcile 字段留痕,并推送告警。

关于"账实不符则暂停执行":本模块只做**检测与告警,不阻断**。原因是权重
偏差里混着两种成因——真正的执行失败(该关注),以及从 T−1 收盘到 T 日
收盘的价格漂移(无害且必然发生,一个 20% 的仓位当天涨 5% 就会带来约 1pp
的权重偏差)。默认容忍度 risk.reconcile_tolerance 因此设在 2pp;是否升级为
硬阻断是策略主人的风控决定,不是本模块的默认。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ReconcileReport:
    ok: bool
    deviations: list = field(default_factory=list)   # [{symbol,target,actual,diff}]
    date: str = ""          # 被对账的执行日
    skipped: str = ""       # 非空 = 未能对账的原因(无 fills / 找不到对应 targets)

    def as_dict(self) -> dict:
        """写进 targets payload 的留痕形态(面板据此展示)。"""
        return {"date": self.date, "ok": self.ok, "skipped": self.skipped,
                "deviations": self.deviations}


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

    day = str(targets_payload.get("date", "") or "")
    if devs and notify_fn is not None:
        lines = "; ".join(
            f"{d['symbol']} 目标{d['target']:.2%} 实际{d['actual']:.2%}"
            for d in devs)
        notify_fn(f"[对账告警] {day or '?'} "
                  f"偏差超阈值({tolerance:.2%}): {lines}")
    return ReconcileReport(ok=not devs, deviations=devs, date=day)


def reconcile_fills(
    fills: dict | None,
    targets_dir,
    *,
    strategy: str,
    tolerance: float,
    notify_fn: Callable | None = None,
) -> ReconcileReport:
    """对一份 fills 与它自报执行日的 targets 做对账(run_signal 的接线入口)。

    影子模式(无 fills)与"薄壳跑了但那天没生成 targets"都不是异常,
    记 skipped 原因返回,不告警——把噪声留给真正的账实不符。
    """
    from .targets import targets_path

    if not fills:
        return ReconcileReport(ok=True, skipped="无 fills 回报(影子模式)")
    day = str(fills.get("date", "") or "")
    if not day:
        return ReconcileReport(ok=True, skipped="fills 未标注执行日")

    fp = targets_path(targets_dir, strategy, day)
    if not fp.exists():
        return ReconcileReport(ok=True, date=day,
                               skipped=f"该执行日无对应 targets({fp.name})")
    try:
        payload = json.loads(Path(fp).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return ReconcileReport(ok=True, date=day, skipped=f"targets 无法解析: {e}")

    return reconcile(payload, fills.get("weights") or {},
                     tolerance=tolerance, notify_fn=notify_fn)
