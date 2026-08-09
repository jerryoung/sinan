#!/usr/bin/env python3
"""
定投(DCA)回测 —— 篮子 × 频率矩阵,与一次性买入对照。

定投是**持续现金流入**,与引擎的固定本金口径不同,收益必须用
资金加权口径衡量,故独立实现:

- 每期(周/月/季首个交易日)固定金额,等分买入篮子内标的(后复权价,
  单边成本 5bp;不做再平衡——定投的常规做法);
- 年化收益 = 现金流 IRR:解 Σ 投入_i × (1+r)^{年数_i} = 期末市值;
- 最大浮亏 = min(市值 ÷ 累计投入 − 1):定投者真实体感的最深水下;
- 对照:同一篮子期初一次性投入同等总额的买入持有(年化 TWR)。

用法:
    python3 scripts/dca_backtest.py [--start 2015-08-03] [--end 2026-08-07]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COST = 0.0005          # 单边成本(ETF 口径)
ANNUAL_BUDGET = 48_000  # 年投入预算,按频率均分:周≈923 / 月 4000 / 季 12000

BASKETS = {
    "中国·沪深300":       ["510300"],
    "中国·三宽基":        ["510300", "510500", "159915"],
    "中国+纳指":          ["510300", "159941"],
    "中国+纳指+黄金":     ["510300", "159941", "518880"],
    "全球五分散":         ["510300", "159941", "513500", "513030", "518880"],
    "全球五分散+国债":    ["510300", "159941", "513500", "513030", "518880", "511010"],
}
FREQS = {"W": ("周定投", 52), "M": ("月定投", 12), "Q": ("季定投", 4)}


def period_firsts(cal, freq):
    """交易日历 → 每周/月/季的首个交易日。"""
    import pandas as pd
    s = pd.Series(cal, index=cal)
    if freq == "W":
        key = s.dt.isocalendar().year.astype(str) + "-" + \
              s.dt.isocalendar().week.astype(str)
    elif freq == "M":
        key = s.dt.year.astype(str) + "-" + s.dt.month.astype(str)
    else:
        key = s.dt.year.astype(str) + "-" + ((s.dt.month - 1) // 3).astype(str)
    return s.groupby(key.values).min().sort_values().tolist()


def irr_annual(flows, fv, end):
    """解 Σ amt×(1+r)^yrs = fv 的年化 r(二分)。flows: [(date, amt)]。"""
    yrs = [(end - d).days / 365.25 for d, _ in flows]
    amts = [a for _, a in flows]

    def f(r):
        return sum(a * (1 + r) ** y for a, y in zip(amts, yrs)) - fv

    lo, hi = -0.95, 3.0
    if f(lo) * f(hi) > 0:
        return float("nan")
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def main() -> int:
    ap = argparse.ArgumentParser(description="定投篮子×频率矩阵回测")
    ap.add_argument("--start", default="2015-08-03")   # 159941 上市后首个周一
    ap.add_argument("--end", default="2026-08-07")
    args = ap.parse_args()

    import numpy as np
    import pandas as pd

    from sinan.config import load_settings
    from sinan.data.store import DataStore

    store = DataStore(load_settings().store_root)
    all_syms = sorted({s for b in BASKETS.values() for s in b})
    bars = store.read_bars(symbols=all_syms, sec_type="etf",
                           start=args.start, end=args.end, adjust=True)
    px = (bars.pivot_table(index="date", columns="symbol", values="close")
          .sort_index().ffill())
    cal = pd.DatetimeIndex(px.index)
    end = cal[-1]

    rows = []
    for bname, syms in BASKETS.items():
        miss = [s for s in syms if s not in px.columns or px[s].isna().all()]
        if miss:
            print(f"跳过 {bname}: 缺 {miss}")
            continue
        p = px[syms].dropna()
        for fq, (flabel, n_per_year) in FREQS.items():
            amt = ANNUAL_BUDGET / n_per_year
            buys = [d for d in period_firsts(pd.DatetimeIndex(p.index), fq)]
            units = pd.Series(0.0, index=syms)
            invested_steps = []
            for d in buys:
                per_sym = amt / len(syms)
                units += per_sym * (1 - COST) / p.loc[d]
                invested_steps.append((d, amt))
            value = (p * units).sum(axis=1)           # 期末口径的每日市值路径
            # 精确浮亏需逐日累计 units;用近似:逐日重演
            units_t = pd.DataFrame(0.0, index=p.index, columns=syms)
            cum_in = pd.Series(0.0, index=p.index)
            u = pd.Series(0.0, index=syms)
            ci = 0.0
            bi = 0
            for d in p.index:
                if bi < len(buys) and d == buys[bi]:
                    u = u + (amt / len(syms)) * (1 - COST) / p.loc[d]
                    ci += amt
                    bi += 1
                units_t.loc[d] = u
                cum_in.loc[d] = ci
            val_t = (units_t * p).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                float_dd = (val_t / cum_in - 1).replace([np.inf, -np.inf], np.nan)
            total_in = ci
            fv = float(val_t.iloc[-1])
            r = irr_annual(invested_steps, fv, end)
            rows.append([bname, flabel, len(buys), total_in, fv,
                         fv / total_in - 1, r, float(float_dd.min())])
        # 一次性对照:期初投入同等总额(按季定投口径的总额)
        total = ANNUAL_BUDGET * (end - p.index[0]).days / 365.25
        mult = float((p.iloc[-1] / p.iloc[0]).mean()) * (1 - COST)
        yrs = (end - p.index[0]).days / 365.25
        rows.append([bname, "一次性买入持有", 1, total, total * mult,
                     mult - 1, mult ** (1 / yrs) - 1,
                     float(((p / p.cummax()).mean(axis=1) - 1).min())])

    df = pd.DataFrame(rows, columns=["篮子", "方式", "期数", "总投入", "期末市值",
                                     "累计收益", "年化IRR", "最大浮亏"])
    pd.set_option("display.unicode.east_asian_width", True)
    print(df.to_string(index=False, formatters={
        "总投入": lambda x: f"{x:,.0f}", "期末市值": lambda x: f"{x:,.0f}",
        "累计收益": lambda x: f"{x*100:6.1f}%", "年化IRR": lambda x: f"{x*100:5.1f}%",
        "最大浮亏": lambda x: f"{x*100:6.1f}%"}))
    out = ROOT / "var" / "reports" / "dca_matrix.csv"
    df.to_csv(out, index=False)
    print(f"\n结果已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
