"""策略账本:净值序列与绩效统计——实盘/影子共用同一套统计口径。

第一性原理:看板只需要一条"策略净值序列",其余(收益统计、回撤、曲线)
全部由它派生。序列来源分两层,谁的真相多听谁的:
  - 实盘/模拟盘:QMT 薄壳每日回写的 fills_{策略}_{日期}.json,
    total_asset 就是账户真值(含费用、滑点、真实成交);
  - 影子模式:无 fills 时,用历次 targets 的权重对 store 后复权收益做
    分段重放(执行日收盘生效,现金按 0 计息、不计费)——近似值,
    与回测引擎口径的差异即"影子 vs 回测"比对的素材。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# 实盘序列(fills)
# --------------------------------------------------------------------------
def load_fills(fills_dir: str | Path, strategy: str) -> list[dict]:
    """按日期升序返回该策略的全部 fills 回报;无则空列表。"""
    d = Path(fills_dir)
    if not d.exists():
        return []
    out = []
    for fp in sorted(d.glob(f"fills_{strategy}_*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:                    # noqa: BLE001 坏文件跳过不阻塞看板
            continue
    return out


def live_nav(fills: list[dict]) -> pd.Series:
    """账户总资产序列 → 净值(首日=1)。同日多份取最后写入。"""
    rows = {}
    for f in fills:
        try:
            rows[pd.Timestamp(f["date"])] = float(f["total_asset"])
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series(rows).sort_index()
    return (s / s.iloc[0]).rename("nav")


def live_trades(fills: list[dict]) -> pd.DataFrame:
    """历日委托记录平铺:date, symbol, side, qty, price。"""
    rows = []
    for f in fills:
        for o in f.get("fills") or []:
            rows.append({"date": f.get("date"), **o})
    return pd.DataFrame(rows, columns=["date", "symbol", "side", "qty", "price"])


# --------------------------------------------------------------------------
# 影子序列(targets 分段重放)
# --------------------------------------------------------------------------
def load_targets_history(targets_dir: str | Path, strategy: str) -> list[dict]:
    """按执行日升序返回该策略的全部 targets payload。"""
    d = Path(targets_dir)
    if not d.exists():
        return []
    out = []
    for fp in sorted(d.glob(f"targets_{strategy}_*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:                    # noqa: BLE001
            continue
    return out


def shadow_nav(store, targets_dir: str | Path, strategy: str,
               sec_type: str = "etf") -> pd.Series:
    """影子净值:第 i 份 targets 的权重自其执行日收盘起生效,
    日收益 = Σ w·r(后复权),现金 0 计息、不计费。不足两份价格日返回空。"""
    hist = load_targets_history(targets_dir, strategy)
    if not hist:
        return pd.Series(dtype=float)
    w_rows = {}
    symbols: set[str] = set()
    for p in hist:
        w = {str(s): float(v) for s, v in (p.get("targets") or {}).items()}
        w_rows[pd.Timestamp(p["date"])] = w
        symbols |= set(w)
    if not symbols:
        return pd.Series(dtype=float)
    bars = store.read_bars(symbols=sorted(symbols), sec_type=sec_type,
                           adjust=True)
    if bars.empty:
        return pd.Series(dtype=float)
    px = (bars.pivot_table(index="date", columns="symbol", values="close")
          .sort_index().ffill())
    start = min(w_rows)
    px = px.loc[px.index >= start]
    if len(px) < 2:
        return pd.Series(dtype=float)
    ret = px.pct_change(fill_method=None).fillna(0.0)
    W = (pd.DataFrame.from_dict(w_rows, orient="index")
         .reindex(columns=px.columns).sort_index()
         .reindex(px.index).ffill().shift(1).fillna(0.0))   # 执行日收盘生效
    port = (W * ret).sum(axis=1)
    nav = (1 + port).cumprod()
    return (nav / nav.iloc[0]).rename("nav")


# --------------------------------------------------------------------------
# 绩效统计(实盘/影子共用)
# --------------------------------------------------------------------------
def _window_ret(nav: pd.Series, offset) -> float | None:
    """截至末日、回看 offset 的区间收益;起点早于序列则返回 None(不足额外推)。"""
    t1 = nav.index[-1]
    t0 = t1 - offset
    if t0 < nav.index[0]:
        return None
    base = nav.loc[:t0]
    if base.empty:
        return None
    return float(nav.iloc[-1] / base.iloc[-1] - 1)


def perf_stats(nav: pd.Series, trading_days: int = 244) -> dict:
    """从净值序列派生全部展示指标;序列不足两点返回 {}。

    键:cum/annual/daily(值,日期)/mtd/m1/m3/m6/y1/ytd/mdd/mdd_start/mdd_end;
    不足回看窗口的值为 None(界面显示 "—",不外推)。
    """
    if nav is None or len(nav) < 2:
        return {}
    nav = nav.sort_index()
    n = len(nav)
    cum = float(nav.iloc[-1] / nav.iloc[0] - 1)
    annual = float((nav.iloc[-1] / nav.iloc[0]) ** (trading_days / max(n - 1, 1)) - 1)
    daily = float(nav.iloc[-1] / nav.iloc[-2] - 1)
    t1 = nav.index[-1]
    mtd_base = nav.loc[:t1.replace(day=1) - pd.Timedelta(days=1)]
    mtd = float(nav.iloc[-1] / mtd_base.iloc[-1] - 1) if len(mtd_base) else None
    ytd_base = nav.loc[:pd.Timestamp(year=t1.year, month=1, day=1)
                       - pd.Timedelta(days=1)]
    ytd = float(nav.iloc[-1] / ytd_base.iloc[-1] - 1) if len(ytd_base) else None
    dd = nav / nav.cummax() - 1
    mdd = float(dd.min())
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    return {
        "cum": cum, "annual": annual,
        "daily": daily, "daily_date": str(t1.date()),
        "mtd": mtd,
        "m1": _window_ret(nav, pd.DateOffset(months=1)),
        "m3": _window_ret(nav, pd.DateOffset(months=3)),
        "m6": _window_ret(nav, pd.DateOffset(months=6)),
        "y1": _window_ret(nav, pd.DateOffset(years=1)),
        "ytd": ytd,
        "mdd": mdd,
        "mdd_start": str(pd.Timestamp(peak).date()),
        "mdd_end": str(pd.Timestamp(trough).date()),
        "n_days": int(n),
    }
