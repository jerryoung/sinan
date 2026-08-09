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
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JUMP_MAX = 0.11        # 质检:接缝/日内变动阈值(涨跌幅限制 10% + 容差)


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

    import numpy as np
    import pandas as pd

    from sinan.config import load_settings, load_strategy
    from sinan.data.store import DataStore

    settings = load_settings()
    store = DataStore(settings.store_root)
    cfg = load_strategy(args.strategy)
    universe = [str(s) for s in cfg.universe]

    if not args.skip_update:
        import akshare as ak

        last = store.read_bars(symbols=universe, sec_type=cfg.sec_type)["date"].max()
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).date()
        today = date.today()
        if start > today:
            print(f"数据已是最新({last.date()}),跳过更新")
        else:
            print(f"增量拉取 {start} → {today}({len(universe)} 只)...")
            rows, fails = [], []
            for s in universe:
                pre = "sh" if s.startswith(("5", "6")) else "sz"
                try:
                    df = ak.fund_etf_hist_sina(symbol=pre + s)
                    df["date"] = pd.to_datetime(df["date"])
                    df = df[(df["date"] >= pd.Timestamp(start))
                            & (df["date"] <= pd.Timestamp(today))]
                    if len(df):
                        rows.append(df.assign(symbol=s)[
                            ["symbol", "date", "open", "high", "low", "close", "volume"]])
                    time.sleep(0.25)
                except Exception as e:              # noqa: BLE001 逐只兜底,末端统一报告
                    fails.append((s, str(e)[:60]))
            if fails:
                print("拉取失败,终止:", fails)
                return 1
            if rows:
                bars = pd.concat(rows, ignore_index=True)
                for c in ["open", "high", "low", "close", "volume"]:
                    bars[c] = pd.to_numeric(bars[c], errors="coerce")
                last_fac, last_close = {}, {}
                for s in universe:
                    prev = store.read_bars(symbols=[s], sec_type=cfg.sec_type, end=last)
                    last_fac[s] = float(prev["adj_factor"].iloc[-1]) if len(prev) else 1.0
                    last_close[s] = float(prev["close"].iloc[-1]) if len(prev) else np.nan
                bars["adj_factor"] = bars["symbol"].map(last_fac)
                bars["amount"] = np.nan
                bars = bars.sort_values(["symbol", "date"])
                bars["pre_close"] = bars.groupby("symbol")["close"].shift(1)
                head = bars.groupby("symbol")["pre_close"].head(1).index
                bars.loc[head, "pre_close"] = bars.loc[head, "symbol"].map(last_close)

                issues = []
                bad = bars[(bars["high"] < bars["low"] - 1e-9)
                           | (bars["close"] > bars["high"] + 1e-9)
                           | (bars["close"] < bars["low"] - 1e-9)]
                if len(bad):
                    issues.append(f"OHLC 非法 {len(bad)} 行")
                jump = (bars["close"] / bars["pre_close"] - 1).abs()
                if (jump > JUMP_MAX).any():
                    issues.append("跳变>%d%%: %s" % (JUMP_MAX * 100, bars.loc[
                        jump > JUMP_MAX, ["symbol", "date"]].to_dict("records")))
                if issues:
                    print("质检未通过,不入库不出信号:", issues)
                    return 1
                store.write_bars(bars, cfg.sec_type)
                store.write_calendar(sorted(bars["date"].unique()))
                print(f"入库 {len(bars)} 行 / {bars['symbol'].nunique()} 只,"
                      f"最新 {bars['date'].max().date()},质检通过")
            else:
                print("区间内无新数据(节假日/周末)")

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
