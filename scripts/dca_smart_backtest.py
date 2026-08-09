#!/usr/bin/env python3
"""
智能定投(下跌加码)对比 —— "价格低于历史平均时多投"的量化检验。

规则:每月首个交易日,逐标的比较 当日后复权价 / 自身 250 日均线(年线):
    基准 1×            无论涨跌固定投入
    跌破年线 2×        价格 < 年线 → 当期该标的投入 ×2,否则 ×1
    跌破 2× / 涨 0.5×  低于年线加倍、高于年线减半(省下的钱留在场外)
    分档 3×/2×/1×      低于年线 10% 以上 ×3,低于年线 ×2,其余 ×1
年线未形成(上市初期)按 1× 处理。总投入随行情浮动,故对比看
IRR(资金加权年化)与最大浮亏,总投入/期末市值仅供参考。

用法: python3 scripts/dca_smart_backtest.py [--start 2015-08-03] [--end 2026-08-07]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dca_backtest import COST, irr_annual, period_firsts   # noqa: E402

MA_N = 250
BASE_MONTHLY = 4_000

BASKETS = {
    "沪深300":         ["510300"],
    "中国+纳指+黄金":  ["510300", "159941", "518880"],
    "全球五分散":      ["510300", "159941", "513500", "513030", "518880"],
}
VARIANTS = {
    "基准 1×":            lambda r: 1.0,
    "跌破年线 2×":        lambda r: 2.0 if r < 1.0 else 1.0,
    "跌 2× / 涨 0.5×":    lambda r: 2.0 if r < 1.0 else 0.5,
    "分档 3×/2×/1×":      lambda r: 3.0 if r < 0.9 else (2.0 if r < 1.0 else 1.0),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="下跌加码定投对比")
    ap.add_argument("--start", default="2015-08-03")
    ap.add_argument("--end", default="2026-08-07")
    args = ap.parse_args()

    import numpy as np
    import pandas as pd

    from sinan.config import load_settings
    from sinan.data.store import DataStore

    store = DataStore(load_settings().store_root)
    all_syms = sorted({s for b in BASKETS.values() for s in b})
    bars = store.read_bars(symbols=all_syms, sec_type="etf",
                           end=args.end, adjust=True)      # 不设 start:年线要暖机
    px_full = (bars.pivot_table(index="date", columns="symbol", values="close")
               .sort_index().ffill())
    ma_full = px_full.rolling(MA_N).mean()
    px = px_full.loc[args.start:]
    ma = ma_full.loc[args.start:]
    end = pd.Timestamp(px.index[-1])

    rows = []
    for bname, syms in BASKETS.items():
        p = px[syms].dropna(how="all")
        buys = period_firsts(pd.DatetimeIndex(p.dropna().index), "M")
        base_sym = BASE_MONTHLY / len(syms)
        for vname, mult_fn in VARIANTS.items():
            u = pd.Series(0.0, index=syms)
            flows, ci = [], 0.0
            units_path, cum_path = [], []
            bi = 0
            for d in p.index:
                if bi < len(buys) and d == buys[bi]:
                    spent = 0.0
                    for s in syms:
                        pxd = p.at[d, s]
                        mad = ma.at[d, s] if s in ma.columns else np.nan
                        if not np.isfinite(pxd):
                            continue
                        mult = mult_fn(pxd / mad) if np.isfinite(mad) else 1.0
                        amt = base_sym * mult
                        u[s] += amt * (1 - COST) / pxd
                        spent += amt
                    if spent > 0:
                        flows.append((d, spent))
                        ci += spent
                    bi += 1
                units_path.append(u.copy())
                cum_path.append(ci)
            val = pd.Series([(uu * p.loc[d]).sum() for uu, d in
                             zip(units_path, p.index)], index=p.index)
            cum = pd.Series(cum_path, index=p.index)
            with np.errstate(invalid="ignore", divide="ignore"):
                fdd = (val / cum - 1).replace([np.inf, -np.inf], np.nan)
            fv = float(val.iloc[-1])
            rows.append([bname, vname, ci, fv, fv / ci - 1,
                         irr_annual(flows, fv, end), float(fdd.min())])

    df = pd.DataFrame(rows, columns=["篮子", "规则", "总投入", "期末市值",
                                     "累计收益", "年化IRR", "最大浮亏"])
    pd.set_option("display.unicode.east_asian_width", True)
    print(df.to_string(index=False, formatters={
        "总投入": lambda x: f"{x:,.0f}", "期末市值": lambda x: f"{x:,.0f}",
        "累计收益": lambda x: f"{x*100:6.1f}%", "年化IRR": lambda x: f"{x*100:5.1f}%",
        "最大浮亏": lambda x: f"{x*100:6.1f}%"}))
    out = ROOT / "var" / "reports" / "dca_smart_matrix.csv"
    df.to_csv(out, index=False)
    print(f"\n结果已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
