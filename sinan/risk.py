"""组合层风险原语——回测引擎与实盘执行层共用的裁剪规则。

这里只放"与通道无关"的规则:同一份实现同时被 backtest/engine.py 的逐日
循环与 live/targets.py 的 apply_risk 调用,回测–实盘一致性才不靠纪律维持。

放在中立模块而非 live/ 包:研究层不应依赖实盘层(否则未来按券商拆分
live/ 时,回测引擎会被一起拖走)。live 与 backtest 同向依赖本模块。
"""
from __future__ import annotations


def limit_positions(targets: dict, current: dict, max_n: int) -> tuple[dict, list[str]]:
    """
    持仓数上限(原版海龟 12-unit 总限的组合版)。目标权重 > 0 的标的数
    超过 max_n 时:**已持仓者优先保留**(不因新信号权重更大而被挤出,
    避免无谓换手),空余名额按目标权重降序分给新入场者;并列按代码升序,
    裁剪确定性。被裁标的目标置 0。max_n <= 0 不限。
    回测引擎与实盘 apply_risk 调用同一实现 —— 回测–实盘一致性(§9)。
    """
    pos = {s: w for s, w in targets.items() if w > 0}
    if max_n <= 0 or len(pos) <= max_n:
        return dict(targets), []
    held = sorted((s for s in pos if current.get(s, 0.0) > 0),
                  key=lambda s: (-pos[s], s))
    new = sorted((s for s in pos if current.get(s, 0.0) <= 0),
                 key=lambda s: (-pos[s], s))
    keep = set((held + new)[:max_n])
    out, msgs = dict(targets), []
    for s in sorted(pos):
        if s not in keep:
            msgs.append(f"持仓数上限: {s} {pos[s]:.4f}→0 (max_positions={max_n})")
            out[s] = 0.0
    return out, msgs
