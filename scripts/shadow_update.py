#!/usr/bin/env python3
"""
影子模式一键链路:增量拉数(新浪端点)→ 质检 → 入库 → 出目标仓位。

数据源现状(2026-08 实测):akshare 东财端点被网络拒绝、tushare 新 token
无 fund_daily 权限,ETF 日线走 akshare 新浪端点(未复权,无 amount,
复权因子按"沿用前值"ffill——若缺口期内有分红会被当作真实下跌,
质检的跳变检查兜底,残余风险很小)。

用法:
    python3 scripts/shadow_update.py                       # 默认 combo 配置
    python3 scripts/shadow_update.py --strategy config/strategies/xxx.yaml \
        --exec-date 2026-08-11
exec-date 缺省 = 数据最新日的下一个工作日(周末顺延,未剔节假日——
节假日当天薄壳空转无害)。
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main() -> int:
    ap = argparse.ArgumentParser(description="影子模式:更新数据并生成目标仓位")
    ap.add_argument("--strategy",
                    default=str(ROOT / "config" / "strategies" / "combo_turtle_xsmom_x2.yaml"))
    ap.add_argument("--exec-date", default=None,
                    help="targets 执行日;缺省=数据最新日的下一工作日")
    ap.add_argument("--skip-update", action="store_true", help="只出信号不更新数据")
    ap.add_argument("--skip-signal", action="store_true",
                    help="只更新数据不出信号(数据中心·数据更新页用)")
    args = ap.parse_args()

    import pandas as pd

    from sinan.config import load_settings, load_strategy
    from sinan.data.store import DataStore

    settings = load_settings()
    store = DataStore(settings.store_root)
    cfg = load_strategy(args.strategy)
    universe = [str(s) for s in cfg.universe]

    if not args.skip_update:
        # 共享增量模块逐标的拉取+质检;影子链路语义严格:任一失败即中止不出信号
        from sinan.data.sina_feed import fetch_incremental
        result = fetch_incremental(store, universe, sec_type=cfg.sec_type)
        if result["failed"]:
            print("拉取/质检失败,终止(不出信号):")
            for f in result["failed"]:
                print(f"  {f['symbol']} {f['start']}→{f['end']}:{f['error']}")
            return 1
        if not result["updated"]:
            print(f"数据已是最新({result['latest']})")

    cutoff = store.read_bars(symbols=universe, sec_type=cfg.sec_type)["date"].max()
    if args.skip_signal:
        print(f"数据截止 {cutoff.date()},按 --skip-signal 跳过信号生成")
        return 0
    if args.exec_date:
        exec_date = args.exec_date
    else:
        d = pd.Timestamp(cutoff) + pd.offsets.BDay()
        exec_date = str(d.date())
    print(f"数据截止 {cutoff.date()},执行日 {exec_date},生成 targets...")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_signal.py"),
                        "--strategy", args.strategy, "--date", exec_date],
                       cwd=ROOT)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
