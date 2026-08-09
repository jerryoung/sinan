"""新浪 ETF 日线增量拉取(shadow_update 与 nightly_update 共用)。

逐标的按各自的最后日期增量拉取(比"全池统一起点"更稳:落后的标的自动
补齐缺口);逐标的质检(OHLC 合法性 + 与前收盘的跳变检查),失败的标的
不入库、原因逐条记录——调用方决定"部分成功继续"(夜间更新)还是
"有失败即中止"(影子出信号前的严格门)。

复权因子语义:增量行沿用该标的最后一个因子(缺口期内若有分红会被当作
真实下跌,跳变检查兜底);全新标的请先走 ensure_bars 全量补数。
"""
from __future__ import annotations

import time
from datetime import datetime

import numpy as np
import pandas as pd

JUMP_MAX = 0.11        # 跳变阈值:涨跌幅限制 10% + 容差
_PAUSE_S = 0.25        # 逐只限速


def fetch_incremental(store, symbols, sec_type: str = "etf",
                      pause: float = _PAUSE_S, log=print) -> dict:
    """增量拉取并入库,返回结构化结果(供更新日志/页面展示):

    {run_at, updated: [sym], rows, latest, up_to_date: [sym],
     failed: [{symbol, start, end, error}]}
    """
    import akshare as ak

    symbols = [str(s) for s in symbols]
    today = pd.Timestamp.today().normalize()
    prev = store.read_bars(symbols=symbols, sec_type=sec_type)
    tail = (prev.sort_values("date").groupby("symbol").tail(1)
            .set_index("symbol") if len(prev) else pd.DataFrame())

    ok_frames, updated, up_to_date, failed = [], [], [], []
    for s in symbols:
        if s not in tail.index:
            failed.append({"symbol": s, "start": "-", "end": str(today.date()),
                           "error": "store 无该标的历史,请先在数据更新页手动补数(全量)"})
            continue
        last = pd.Timestamp(tail.loc[s, "date"])
        start = last + pd.Timedelta(days=1)
        if start > today:
            up_to_date.append(s)
            continue
        pre = "sh" if s.startswith(("5", "6")) else "sz"
        try:
            df = ak.fund_etf_hist_sina(symbol=pre + s)
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"] >= start) & (df["date"] <= today)]
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date")
            if not len(df):
                up_to_date.append(s)          # 区间无新数据(节假日/周末)
                time.sleep(pause)
                continue
            bad = df[(df["high"] < df["low"] - 1e-9)
                     | (df["close"] > df["high"] + 1e-9)
                     | (df["close"] < df["low"] - 1e-9)]
            if len(bad):
                raise ValueError(f"OHLC 非法 {len(bad)} 行")
            closes = pd.concat([pd.Series([float(tail.loc[s, "close"])]),
                                df["close"]], ignore_index=True)
            jump = closes.pct_change().abs().iloc[1:]
            if (jump > JUMP_MAX).any():
                d = df["date"].iloc[int(jump.values.argmax())].date()
                raise ValueError(f"跳变 {jump.max():.1%} 于 {d}(> {JUMP_MAX:.0%})")
            fac = float(tail.loc[s, "adj_factor"]) if "adj_factor" in tail else 1.0
            ok_frames.append(df.assign(symbol=s, adj_factor=fac, amount=np.nan)[
                ["symbol", "date", "open", "high", "low", "close",
                 "volume", "adj_factor", "amount"]])
            updated.append(s)
        except Exception as e:                 # noqa: BLE001 逐只兜底,末端统一报告
            failed.append({"symbol": s, "start": str(start.date()),
                           "end": str(today.date()), "error": str(e)[:120]})
        time.sleep(pause)

    rows = 0
    latest = str(tail["date"].max().date()) if len(tail) else "-"
    if ok_frames:
        bars = pd.concat(ok_frames, ignore_index=True)
        store.write_bars(bars, sec_type)
        store.write_calendar(sorted(bars["date"].unique()))
        rows = len(bars)
        latest = str(bars["date"].max().date())
        log(f"入库 {rows} 行 / {len(updated)} 只,最新 {latest}")
    if failed:
        for f in failed:
            log(f"[失败] {f['symbol']} {f['start']}→{f['end']}:{f['error']}")
    return {"run_at": datetime.now().isoformat(timespec="seconds"),
            "updated": updated, "rows": rows, "latest": latest,
            "up_to_date": up_to_date, "failed": failed}
