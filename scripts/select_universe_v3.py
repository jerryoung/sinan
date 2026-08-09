#!/usr/bin/env python3
"""
v3 规则化选样 —— 把老脚本 etf_trend_v3_dedup.pick_basket 复刻到新数据仓。

选样规则(与老脚本逐条对齐,差异已注明):

1. 交易域 = ../data/etf_dedup_universe.csv(670 只,每个跟踪指数只留
   成交额最大的一只)—— 消除同指数多产品的伪分散;
2. 剔除货币/存单类:截止日前年化波动 < 2% 无趋势可言
   (老脚本用全历史波动,此处收紧为截止日前 —— 消除这一点选样前视,
   对货币类判定无实质差异);
3. 候选资格:上市 ≤ CUT(默认 2021-07-01)且 CUT 前 ≥ 200 根;
4. 类内按 CUT 前日均成交额降序,大类配额
   宽基4 / 行业主题6 / 跨境4 / 商品3 / 债券1 / 策略2 = 20 只;
5. 相关约束:候选与任一已选标的 CUT 前日收益 ρ > 0.75 则剔除
   (≥200 个重叠交易日才判)—— 纯流动性配额会挑进创业板50/深证100
   这类高相关线,低相关约束才买到真分散(老 v3 结论)。

用法:
    python3 scripts/select_universe_v3.py                 # 打印选样
    python3 scripts/select_universe_v3.py --write-yaml    # 另生成 *_etf_v3.yaml
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRADING_DAYS = 244
ETF_MIN_VOL = 0.02
CORR_MAX = 0.75
MIN_DAYS = 200
CUT = "2021-07-01"
QUOTA = {"宽基": 4, "行业/主题": 6, "跨境": 4, "商品": 3, "债券": 1, "策略": 2}


def pick_basket(cands, series, *, quota, corr_max=CORR_MAX, min_days=MIN_DAYS):
    """
    去重域内按大类配额 + 流动性 + 相关性约束选样(纯函数,供测试)。

    cands:  [(code, category), ...]
    series: {code: DataFrame(index=date, columns 含 close/amount)},
            调用方须已截断到 CUT —— 本函数看不到截止日之后的数据。
    返回按选出顺序的 code 列表。
    """
    import pandas as pd

    rets: dict[str, "pd.Series"] = {}
    picked: list[str] = []
    for cat, k in quota.items():
        rows = []
        for code, c in cands:
            if c != cat or code not in series:
                continue
            g = series[code]
            if len(g) >= min_days:
                rows.append((code, float(g["amount"].mean())))
        rows.sort(key=lambda x: -x[1])
        n = 0
        for code, _ in rows:
            if n >= k:
                break
            r = series[code]["close"].pct_change()
            ok = True
            for p in picked:
                both = pd.concat([r, rets[p]], axis=1).dropna()
                if len(both) >= min_days and both.corr().iloc[0, 1] > corr_max:
                    ok = False
                    break
            if ok:
                picked.append(code)
                rets[code] = r
                n += 1
    return picked


def run_selection(store, dedup_csv, cut, *, quota=None, verbose=True):
    """
    完整选样流程(供 main 与滚动重选脚本复用):
    读去重域 → store 取截至 cut 的后复权行情 → 低波剔除 → pick_basket。
    返回 (picked, names, cat_of)。
    """
    import numpy as np
    import pandas as pd

    quota = quota or QUOTA
    cut = pd.Timestamp(cut)
    uni = pd.read_csv(dedup_csv)
    uni["symbol"] = uni["ts_code"].str.split(".").str[0]

    bars = store.read_bars(symbols=list(uni["symbol"]), sec_type="etf",
                           end=cut, adjust=True)
    series, dropped_vol = {}, 0
    for sym, g in bars.groupby("symbol"):
        g = g.set_index("date").sort_index()
        vol = g["close"].pct_change().std() * np.sqrt(TRADING_DAYS)
        if np.isnan(vol) or vol < ETF_MIN_VOL:
            dropped_vol += 1
            continue
        series[str(sym)] = g[["close", "amount"]]
    if verbose:
        print(f"候选 {uni.shape[0]} 只;截止 {cut.date()} 有行情 "
              f"{bars['symbol'].nunique()} 只,剔除货币/低波 {dropped_vol} 只")

    picked = pick_basket(list(zip(uni["symbol"], uni["category"])), series,
                         quota=quota)
    return (picked, dict(zip(uni["symbol"], uni["name"])),
            dict(zip(uni["symbol"], uni["category"])))


def main() -> int:
    ap = argparse.ArgumentParser(description="v3 规则化选样(去重域 20-ETF)")
    ap.add_argument("--cut", default=CUT, help="选样信息截止日")
    ap.add_argument("--dedup-csv",
                    default=str(ROOT.parent / "data" / "etf_dedup_universe.csv"))
    ap.add_argument("--write-yaml", action="store_true",
                    help="生成 config/strategies/{donchian,turtle_s1}_etf_v3.yaml")
    args = ap.parse_args()

    from sinan.config import load_settings
    from sinan.data.store import DataStore

    store = DataStore(load_settings().store_root)
    picked, names, cat_of = run_selection(store, args.dedup_csv, args.cut)
    print(f"\nv3 规则化选样 {len(picked)} 只(配额 {QUOTA}):")
    for s in picked:
        print(f"  [{cat_of[s]}] {s} {names.get(s, '')}")

    if args.write_yaml:
        M = len(picked)
        cap = round(1.0 / M, 6)
        x_risk = round(0.35 / 8 / M, 6)
        ulist = ", ".join(f'"{s}"' for s in picked)
        # 非默认截止日 → 文件名带 cut 后缀,避免覆盖基准 v3 配置
        sfx = "" if args.cut == CUT else "_cut" + args.cut.replace("-", "")[:8]
        specs = {
            f"donchian_etf_v3{sfx}": ("donchian", [
                "n_entry: 55", "n_exit: 20", "atr_n: 20", "atr_m: 3.0",
                f"x_risk: {x_risk}", f"cap: {cap}", "exit_mode: atr"]),
            f"turtle_s1_etf_v3{sfx}": ("turtle_s1", [
                "n_entry: 20", "n_exit: 10", "n_failsafe: 55", "atr_n: 20",
                "stop_n_mult: 2.0", f"x_risk: {x_risk}", f"cap: {cap}",
                "use_filter: true"]),
        }
        for name, (strat, params) in specs.items():
            fp = ROOT / "config" / "strategies" / f"{name}.yaml"
            body = "\n".join(f"  {p}" for p in params)
            fp.write_text(
                f"# v3 规则化选样篮子(scripts/select_universe_v3.py 生成,"
                f"CUT={args.cut},M={M});\n"
                f"# 等分槽口径:cap=1/M、x_risk=(0.35/8)/M。选样规则见脚本 docstring。\n"
                f"name: {name}\nstrategy: {strat}\nsec_type: etf\n"
                f"universe: [{ulist}]\nlookback: 750\nparams:\n{body}\n",
                encoding="utf-8")
            print(f"已写 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
