#!/usr/bin/env python3
"""
盘中 14:35 生成目标仓位(方案 §8.1)——影子模式与实盘共用的信号入口。

流程:从 store 读后复权 bars 构造 SignalContext(与回测引擎喂同一个
generate_targets,这就是回测–实盘一致性)→ 执行层强制风控 apply_risk
→ 写 targets_YYYYMMDD.json → 企业微信推送目标仓位摘要。

当前持仓取 QMT 薄壳最近回写的 fills(影子模式无 fills 则视为空仓、
名义资产用 --total-asset)。

用法:
    python3 scripts/run_signal.py
    python3 scripts/run_signal.py --strategy config/strategies/donchian_etf.yaml \
        --date 2026-08-04 --total-asset 1000000
"""
import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADV_WINDOW = 20   # 流动性检查:近 20 日均成交额


def main() -> int:
    ap = argparse.ArgumentParser(description="生成当日目标仓位 targets 文件")
    ap.add_argument("--strategy",
                    default=str(ROOT / "config" / "strategies" / "donchian_etf.yaml"))
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--total-asset", type=float, default=None,
                    help="影子模式(无 fills 回报)下的名义总资产;缺省用 settings.capital")
    args = ap.parse_args()

    import pandas as pd

    from sinan.config import (load_live_profiles, load_rules, load_settings,
                              load_strategy, resolve_live_profile)
    from sinan.data.store import DataStore
    from sinan.live.broker import QmtShellBroker
    from sinan.live.notify import notify
    from sinan.live.reconcile import reconcile_fills
    from sinan.live.targets import (apply_risk, build_payload, build_ref_orders,
                                    save_targets)
    from sinan.signal.base import SignalContext, call_strategy
    from sinan.universe.cb_terms import build_cb_terms
    from sinan.universe.instruments import resolve_rule

    settings = load_settings()
    cfg = load_strategy(args.strategy)
    profile_id, live_profile = resolve_live_profile(load_live_profiles(), cfg)
    qmt_cfg = live_profile.qmt.targets_payload()
    store = DataStore(settings.store_root)
    today = pd.Timestamp(args.date)

    def notify_fn(msg, **kw):
        notify(msg, webhook=settings.wecom_webhook, **kw)

    # 1) 后复权 bars(与回测同一数据口径),截至 today —— 无未来函数
    bars = store.read_bars(symbols=cfg.universe, sec_type=cfg.sec_type,
                           end=today, adjust=True)
    if bars.empty:
        notify_fn(f"[信号] {args.date} 无可用行情数据,拒绝生成 targets",
                  level="error")
        return 1
    data = {sym: g.set_index("date").sort_index()
            for sym, g in bars.groupby("symbol")}
    data_cutoff = bars["date"].max()

    # 2) 交易规则与(转债)条款
    inst = store.read_instruments(sec_type=cfg.sec_type)
    names = dict(zip(inst["symbol"], inst["name"])) if len(inst) else {}
    rules_cfg = load_rules()
    rules = {s: resolve_rule(s, cfg.sec_type, rules_cfg, name=names.get(s, ""))
             for s in cfg.universe}
    cb = None
    if cfg.sec_type == "cb":
        cb = build_cb_terms(store.read_cb_terms(cfg.universe),
                            store.read_cb_events(cfg.universe))

    # 3) 当前持仓:最近一份 fills 回报;影子模式下为空仓
    # 本金优先级:CLI --total-asset > 策略 YAML capital > 全局 settings.capital
    positions = {}
    total_asset = args.total_asset or cfg.capital or settings.capital
    last = QmtShellBroker.latest_fills(settings.fills_dir, cfg.name)
    if last:
        positions = {s: float(w) for s, w in (last.get("weights") or {}).items()}
        total_asset = float(last.get("total_asset") or total_asset)

    # 3.5) 对账:上一执行日的 targets vs 其 fills —— 文件桥接模式的安全网。
    # 在这里做而不是另起 15:10 定时任务:今天该不该照常下单,取决于上一次
    # 到底执行成没成。仅告警不阻断(价格漂移也会造成权重偏差,见 reconcile)
    recon = reconcile_fills(last, settings.targets_dir, strategy=cfg.name,
                            tolerance=settings.risk.reconcile_tolerance,
                            notify_fn=lambda m, **kw: notify_fn(m, level="warning"))
    if recon.skipped:
        print(f"对账跳过:{recon.skipped}")
    elif recon.ok:
        print(f"对账通过:{recon.date} 账实一致"
              f"(容忍度 {settings.risk.reconcile_tolerance:.1%})")
    else:
        print(f"对账不符:{recon.date} {len(recon.deviations)} 只标的超容忍度")

    # 4) 同一个 generate_targets(回测/实盘唯一策略实现)。
    #    调用约定(含 lookback 传参)收口在 call_strategy,与引擎同一实现;
    #    ctx 的可见列由 SignalContext 统一裁到 SIG_COLS,两侧列集一致。
    #    不传 window_start:实盘用配置里写死的计划锚点(回测才对齐窗口首日)
    ctx = SignalContext(today=today, data=data, positions=positions,
                        total_asset=total_asset, rules=rules,
                        universe=cfg.universe, cb_terms=cb)
    weights = call_strategy(cfg, ctx)

    # 5) 执行层强制风控:近 20 日均成交额做流动性截断(amount 单位为元)
    adv20 = {}
    for sym, df in data.items():
        amt = df["amount"].tail(ADV_WINDOW).dropna() if "amount" in df else []
        if len(amt):
            adv20[sym] = float(pd.Series(amt).mean())
    weights, risk_msgs = apply_risk(
        weights, current_weights=positions, total_asset=total_asset,
        adv20=adv20 or None, settings=settings)

    # 6) 留痕落盘 + 推送摘要
    # date=执行日(薄壳当日校验的那一天);data_cutoff=信号数据截止,两者可差一天
    payload = build_payload(weights, strategy_name=cfg.name,
                            date=today, data_cutoff=data_cutoff,
                            params_fingerprint=cfg.params,
                            live_profile=profile_id, qmt=qmt_cfg)
    # 参考委托单(仅供影子模式/人工执行;QMT 薄壳仍按实时账户自算差额)
    prices = {s: float(df["close_raw"].iloc[-1]) if "close_raw" in df
              else float(df["close"].iloc[-1]) for s, df in data.items()}
    payload["capital"] = total_asset
    payload["reconcile"] = recon.as_dict()   # 上一执行日的对账结论,随当日决策留痕
    # qmt 仍使用薄壳既有字段协议;live_profile 额外记录命名配置 ID 供追溯
    payload["ref_orders"] = build_ref_orders(
        weights, current_weights=positions, capital=total_asset, prices=prices,
        lot_sizes={s: r.lot_size for s, r in rules.items()},
        band=(cfg.rebalance_band if cfg.rebalance_band is not None
              else settings.execution.rebalance_band))
    path = save_targets(payload, settings.targets_dir)

    lines = [f"{s}: {w:.1%}" for s, w in sorted(weights.items()) if w > 0] or ["空仓"]
    msg = f"[目标仓位] {payload['date']} {cfg.name}\n" + "\n".join(lines)
    if risk_msgs:
        msg += "\n风控裁剪:\n" + "\n".join(risk_msgs)
    notify_fn(msg)
    print(f"targets 已写入: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
