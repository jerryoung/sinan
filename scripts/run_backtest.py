#!/usr/bin/env python3
"""
回测 CLI(方案 §7):跑指定策略与区间,打印中文指标表并输出单文件 HTML 报告。

用法:
    python3 scripts/run_backtest.py --strategy config/strategies/donchian_etf.yaml \
        --start 2015-01-01 --end 2026-08-01
    # --report 缺省为 reports/<策略名>_<start>_<end>.html
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="趋势策略回测")
    ap.add_argument("--strategy",
                    default=str(ROOT / "config" / "strategies" / "donchian_etf.yaml"))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--report", default="",
                    help="HTML 报告输出路径,缺省 reports/<name>_<start>_<end>.html")
    args = ap.parse_args()

    from trend.backtest.engine import run_backtest
    from trend.backtest.report import compute_stats, render_html
    from trend.config import load_settings, load_strategy
    from trend.data.store import DataStore

    settings = load_settings()
    cfg = load_strategy(args.strategy)
    store = DataStore(settings.store_root)

    result = run_backtest(store, cfg, settings, args.start, args.end,
                          initial_capital=cfg.capital or settings.capital)
    stats = compute_stats(result)

    # 中文指标表:标量项对齐打印,表格类(如月度收益)留给 HTML 报告
    print(f"\n==== {cfg.name}  {args.start} → {args.end} ====")
    for k, v in stats.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        print(f"{k:<16}{v:>12.4f}")

    out = Path(args.report) if args.report else (
        settings.reports_dir / f"{cfg.name}_{args.start}_{args.end}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    render_html(result, stats, out)
    print(f"\nHTML 报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
