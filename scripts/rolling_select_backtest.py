#!/usr/bin/env python3
"""
滚动重选(walk-forward)回测 —— 消除 v3 选样对单一截止日的依赖。

方法:每年 7 月 1 日用**截至当日**的信息重选一次篮子(规则与
select_universe_v3 完全相同),只在接下来一年交易该篮子;各年段收益
首尾拼接,得到一条**全程样本外**的净值——任何一天的持仓都不依赖
该日之后的信息。

工程细节(有意的简化,解读时注意):
- 每段独立跑引擎、起点空仓:拼接处等于强制换仓一次,多付一次建仓
  成本并丢失跨段持仓连续性 → 结果相对"真实滚动持仓"**偏保守**;
- 每段的定仓参数随该段实际选出数量 M 换算(cap=1/M、x_risk=(0.35/8)/M);
- 起始截止日默认 2017-07(更早的 ETF 候选池太薄,行业/商品配额凑不齐)。

用法:
    python3 scripts/rolling_select_backtest.py                    # S1+S2 全跑
    python3 scripts/rolling_select_backtest.py --strategies donchian
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TD = 244

STRATEGY_PARAMS = {
    "donchian": dict(n_entry=55, n_exit=20, atr_n=20, atr_m=3.0,
                     exit_mode="atr"),
    "turtle_s1": dict(n_entry=20, n_exit=10, n_failsafe=55, atr_n=20,
                      stop_n_mult=2.0, use_filter=True),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="滚动重选 walk-forward 回测")
    ap.add_argument("--first-cut", default="2017-07-01")
    ap.add_argument("--end", default="2026-07-21")
    ap.add_argument("--strategies", default="donchian,turtle_s1")
    ap.add_argument("--dedup-csv",
                    default=str(ROOT.parent / "data" / "etf_dedup_universe.csv"))
    args = ap.parse_args()

    import pandas as pd

    from select_universe_v3 import run_selection
    from trend.config import StrategyCfg, load_settings
    from trend.backtest.engine import run_backtest
    from trend.data.store import DataStore

    settings = load_settings()
    store = DataStore(settings.store_root)
    end = pd.Timestamp(args.end)

    # 年度截止日序列:first-cut 起每年 7-01,最后一段延伸到 end
    cuts = []
    c = pd.Timestamp(args.first_cut)
    while c < end:
        cuts.append(c)
        c += pd.DateOffset(years=1)

    baskets = {}
    for cut in cuts:
        picked, _, _ = run_selection(store, args.dedup_csv, cut, verbose=False)
        baskets[cut] = picked
        prev = baskets.get(cut - pd.DateOffset(years=1))
        diff = (f"换入 {sorted(set(picked) - set(prev))} "
                f"换出 {sorted(set(prev) - set(picked))}" if prev else "")
        print(f"{cut.date()} 选出 {len(picked)} 只 {diff}")

    results = {}
    for strat in args.strategies.split(","):
        strat = strat.strip()
        segs = []
        for i, cut in enumerate(cuts):
            seg_end = cuts[i + 1] - pd.Timedelta(days=1) if i + 1 < len(cuts) else end
            M = len(baskets[cut])
            cfg = StrategyCfg(
                name=f"roll_{strat}_{cut.date()}", strategy=strat,
                sec_type="etf", universe=baskets[cut], lookback=750,
                params=dict(STRATEGY_PARAMS[strat],
                            x_risk=round(0.35 / 8 / M, 6),
                            cap=round(1.0 / M, 6)))
            r = run_backtest(store, cfg, settings, start=cut, end=seg_end)
            segs.append(r.returns)
            print(f"  {strat} {cut.date()}→{seg_end.date()} 段净值 "
                  f"{(1 + r.returns).prod():.4f}")
        results[strat] = pd.concat(segs)

    print("\n拼接后的全程样本外表现(%s → %s):" % (cuts[0].date(), end.date()))
    for strat, ret in results.items():
        eq = (1 + ret).cumprod()
        yrs = len(ret) / TD
        ann = eq.iloc[-1] ** (1 / yrs) - 1
        mdd = (eq / eq.cummax() - 1).min()
        shp = ret.mean() / ret.std() * (TD ** 0.5) if ret.std() > 0 else 0
        print(f"  {strat:10s}: 年化 {ann*100:5.1f}%  回撤 {mdd*100:6.1f}%  "
              f"夏普 {shp:5.2f}  Calmar {ann/abs(mdd):5.2f}")

    out = ROOT / "reports" / "rolling_walkforward_returns.csv"
    pd.DataFrame(results).to_csv(out)
    print(f"\n逐日收益已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
