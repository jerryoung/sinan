"""按需补数:标的在 store 无行情时先尝试从数据源拉全量补齐,拉不到才报错。

设计约束:回测引擎保持纯离线(不触网),补数只发生在编排层——app 回测页
与 run_backtest CLI 在调用引擎前先经过本模块。ETF 走新浪端点(与影子链路
shadow_update 同源):未复权、adj_factor 从 1.0 起步,此后分红由日更的
ffill 语义延续(与 store 中存量 ETF 同口径)。

质检:OHLC 合法性为硬门槛(不合格不入库);全量历史的涨跌跳变仅告警不拦
——QDII/商品 ETF 无涨跌幅限制,未复权分红也会造成真实跳空,不能一刀切。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

_FETCH_PAUSE_S = 0.25     # 逐只限速,避免触发数据源限流
_JUMP_WARN = 0.20         # 全量历史跳变告警阈值(仅提示,不拦截)


def ensure_bars(store, symbols, sec_type: str = "etf", log=print) -> list[str]:
    """确保 symbols 在 store 有行情;缺失的尝试拉全量入库。

    返回补数后仍缺失的标的列表(空列表 = 全部可用)。只支持 etf 自动补数
    (新浪端点是当前唯一验证可用的免费源);其他品种直接返回缺失清单。
    """
    symbols = [str(s) for s in symbols]
    bars = store.read_bars(symbols=symbols, sec_type=sec_type)
    have = set(bars["symbol"].astype(str).unique()) if len(bars) else set()
    missing = [s for s in symbols if s not in have]
    if not missing:
        return []
    if sec_type != "etf":
        log(f"缺行情且暂不支持自动补数(sec_type={sec_type}):{missing}")
        return missing

    import akshare as ak

    still, rows = [], []
    for s in missing:
        pre = "sh" if s.startswith(("5", "6")) else "sz"
        try:
            df = ak.fund_etf_hist_sina(symbol=pre + s)
            df["date"] = pd.to_datetime(df["date"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date")
            if not len(df):
                raise ValueError("数据源空返回")
            bad = df[(df["high"] < df["low"] - 1e-9)
                     | (df["close"] > df["high"] + 1e-9)
                     | (df["close"] < df["low"] - 1e-9)]
            if len(bad):
                raise ValueError(f"OHLC 非法 {len(bad)} 行,拒绝入库")
            n_jump = int((df["close"].pct_change().abs() > _JUMP_WARN).sum())
            if n_jump:
                log(f"[提示] {s} 全量历史有 {n_jump} 处 >{_JUMP_WARN:.0%} 跳变"
                    "(未复权分红/无涨跌幅限制品种属正常),已放行")
            rows.append(df.assign(symbol=s, adj_factor=1.0, amount=np.nan)[
                ["symbol", "date", "open", "high", "low", "close",
                 "volume", "adj_factor", "amount"]])
            log(f"补数 {s}:{len(df)} 行"
                f"({df['date'].min().date()} → {df['date'].max().date()})")
            time.sleep(_FETCH_PAUSE_S)
        except Exception as e:                     # noqa: BLE001 逐只兜底
            log(f"补数失败 {s}:{str(e)[:80]}")
            still.append(s)
    if rows:
        store.write_bars(pd.concat(rows, ignore_index=True), sec_type)
    return still
