"""策略看板:影子/实盘统一入口。

第一性原理:看板回答四个问题——现在是什么模式、赚了多少、拿着什么、
做过什么。数据来源自动切换:该策略有 QMT fills 回报即实盘口径(模拟/
实盘由薄壳上报的 trade_mode 区分),没有则用 targets 影子重放;
两种口径共用 ledger.perf_stats 同一套统计。顶部精简,细节全部可展开。
"""
import subprocess
import sys

import pandas as pd
import streamlit as st

from sinan.config import load_settings
from sinan.live import ledger
from ui.common import (NAV_COLOR, ROOT, confirm_delete, current_sel,
                       etf_names, get_store, show_targets, targets_files)

_MODE = {"real": "🔴 实盘", "sim": "🟡 模拟盘", "backtest": "⚪ QMT回测",
         "unknown": "🔵 实盘(模式未知)"}


def _fmt(v, digits=2):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


@st.cache_data(ttl=300)
def _shadow_nav_cached(strategy: str, n_targets: int) -> pd.Series:
    """影子净值(n_targets 参与缓存 key:新 targets 生成后自动失效)。"""
    return ledger.shadow_nav(get_store(), load_settings().targets_dir, strategy)


def page():
    sel_g, sel_name, sel_label = current_sel()
    settings = load_settings()
    fills = ledger.load_fills(settings.fills_dir, sel_name)
    live = bool(fills)
    mode = (_MODE.get(str(fills[-1].get("trade_mode", "unknown")),
                      "🔵 实盘") if live else "🛰️ 影子模式")

    h1, h2 = st.columns([3, 1])
    h1.subheader(f"策略看板:{sel_label}")
    h1.caption(f"{mode}"
               + (f" · 账号 {fills[-1].get('account', '—')} · 数据来自 QMT 回报"
                  if live else " · 未接 QMT,净值为 targets 影子重放(不计费,现金 0 计息)"))
    if h2.button("▶ 更新数据并生成 targets", type="primary"):
        with st.spinner("拉数 → 质检 → 出信号(约 1~2 分钟)…"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "shadow_update.py"),
                                "--strategy", str(sel_g)],
                               cwd=ROOT, capture_output=True, text=True)
        st.code((r.stdout + r.stderr)[-3000:] or "(无输出)")
        if r.returncode == 0:
            st.cache_data.clear()
            st.success("完成")
        else:
            st.error("失败——质检未过或数据源不可用,详见日志。未生成新 targets。")

    # ---- 收益统计与曲线(实盘/影子同一套口径)----------------------------
    nav = (ledger.live_nav(fills) if live
           else _shadow_nav_cached(sel_name, len(targets_files(sel_name))))
    stats = ledger.perf_stats(nav)
    if not stats:
        st.info("跟踪数据不足(需要至少两个交易日的净值)。生成 targets 并跟踪"
                "几天后,这里会出现收益统计与净值曲线。")
    else:
        m = st.columns(5)
        m[0].metric("累计收益", _fmt(stats["cum"]))
        m[1].metric("年化收益", _fmt(stats["annual"]))
        m[2].metric(f"日涨幅({stats['daily_date']})", _fmt(stats["daily"]))
        m[3].metric("最大回撤", _fmt(stats["mdd"]))
        m[4].metric("跟踪交易日", f"{stats['n_days']}")
        st.markdown("**策略收益曲线**")
        st.line_chart(((nav - 1) * 100).rename("累计收益 %"),
                      color=NAV_COLOR, height=260)
        with st.expander("策略表现(完整指标)"):
            rows = [("累计收益", _fmt(stats["cum"])),
                    ("年化收益", _fmt(stats["annual"])),
                    (f"日涨幅({stats['daily_date']})", _fmt(stats["daily"])),
                    ("月涨幅(本月)", _fmt(stats["mtd"])),
                    ("近一月", _fmt(stats["m1"])),
                    ("近三月", _fmt(stats["m3"])),
                    ("近六月", _fmt(stats["m6"])),
                    ("近一年", _fmt(stats["y1"])),
                    ("年初至今", _fmt(stats["ytd"])),
                    ("最大回撤", _fmt(stats["mdd"])),
                    ("最大回撤区间", f"{stats['mdd_start']} → {stats['mdd_end']}"),
                    ("跟踪交易日", str(stats["n_days"]))]
            st.dataframe(pd.DataFrame(rows, columns=["指标", "数值"]),
                         width="stretch", hide_index=True)
            st.caption("不足回看窗口的指标显示 '—'(不外推);影子口径不含费用,"
                       "接入 QMT 后自动切换为账户真值。")

    # ---- 持仓详情 --------------------------------------------------------
    st.markdown("**持仓详情**")
    names = etf_names()
    if live:
        last = fills[-1]
        pos = last.get("positions") or {}
        total = float(last.get("total_asset") or 0)
        c = st.columns(3)
        c[0].metric("总资产", f"{total:,.0f} 元")
        c[1].metric("现金", f"{float(last.get('cash') or 0):,.0f} 元")
        c[2].metric("回报日期", str(last.get("date", "—")))
        if pos:
            rows = [{"代码": s, "名称": names.get(s, ""),
                     "数量": f"{p.get('qty', 0):,.0f}",
                     "最新价": p.get("price", 0.0),
                     "市值": f"{p.get('qty', 0) * p.get('price', 0.0):,.0f}",
                     "权重": f"{(last.get('weights') or {}).get(s, 0):.1%}"}
                    for s, p in pos.items()]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("空仓")
    else:
        tfs = targets_files(sel_name)
        if tfs:
            import json
            p = json.loads(tfs[0].read_text(encoding="utf-8"))
            cap = float(p.get("capital") or settings.capital)
            tgt = {k: v for k, v in p["targets"].items() if v > 0}
            if tgt:
                rows = [{"代码": s, "名称": names.get(s, ""), "权重": f"{w:.1%}",
                         "目标金额": f"{cap * w:,.0f}"}
                        for s, w in sorted(tgt.items(), key=lambda kv: -kv[1])]
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                st.caption(f"影子口径:目标仓位 ×本金 {cap:,.0f} 元"
                           f"(执行日 {p['date']});接入 QMT 后显示真实持仓。")
            else:
                st.info("空仓")
        else:
            st.info(f"策略 {sel_label} 尚无 targets,点右上按钮生成一份。")

    # ---- 交易记录 --------------------------------------------------------
    with st.expander("交易记录"):
        if live:
            tr = ledger.live_trades(fills)
            if len(tr):
                tr["名称"] = tr["symbol"].astype(str).map(lambda x: names.get(x, ""))
                tr["side"] = tr["side"].map({"buy": "买入", "sell": "卖出"})
                tr = tr[["date", "symbol", "名称", "side", "qty", "price"]]
                tr.columns = ["日期", "代码", "名称", "方向", "数量", "委托价"]
                st.dataframe(tr.sort_values("日期", ascending=False).head(200),
                             width="stretch", hide_index=True)
                st.caption(f"共 {len(tr)} 笔(QMT 薄壳回报口径,显示最近 200 笔)")
            else:
                st.info("暂无委托记录")
        else:
            hist = ledger.load_targets_history(settings.targets_dir, sel_name)
            rows = []
            for p in hist:
                for o in p.get("ref_orders") or []:
                    rows.append({"执行日": p["date"], "代码": o["symbol"],
                                 "名称": names.get(o["symbol"], ""),
                                 "方向": "买入" if o["side"] == "buy" else "卖出",
                                 "参考股数": o["qty"], "参考价": o["ref_price"],
                                 "预估金额": f"{o['est_amount']:,.0f}"})
            if rows:
                st.dataframe(pd.DataFrame(rows).sort_values("执行日", ascending=False),
                             width="stretch", hide_index=True)
                st.caption("影子口径:历次 targets 的参考委托单(非真实成交)")
            else:
                st.info("暂无参考委托记录")

    # ---- 目标仓位 targets(详细)----------------------------------------
    tfs = targets_files(sel_name)
    with st.expander("目标仓位 targets(最新详情与历史)"):
        if not tfs:
            st.info("尚无 targets 文件")
        else:
            show_targets(tfs[0])
            st.divider()
            s1, s2 = st.columns([4, 1])
            tp = s1.selectbox("历史 targets", tfs, format_func=lambda p: p.name)
            s2.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
            if s2.button("🗑 删除", key="tgt_del"):
                confirm_delete(tp, label="targets 文件")
            if tp != tfs[0]:
                show_targets(tp)
