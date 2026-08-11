"""回测页:一次回测 = 一份报告;页面统一渲染"选中的报告"(默认最新)。"""
import difflib
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from sinan.config import load_settings, load_strategy
from ui.common import (confirm_delete, current_sel, get_store, result_sidecar,
                       save_result_json, show_report)
from ui.theme import page_header, section_title, workflow_bar


_LEGACY_REPORT_CSS = """
<style>
html,body{background:#111A25!important;color:#E8EDF4!important;
font-family:'Source Sans 3','Noto Sans SC','PingFang SC',sans-serif!important}
.container{max-width:none!important;background:transparent!important;box-shadow:none!important}
h1,h2,h3{color:#E8EDF4!important} table{background:#111A25!important;color:#E8EDF4!important}
th{background:#263965!important;color:#F4F6FA!important} td{border-color:#273241!important}
tr:nth-child(even){background:#151F2C!important} p,.note{color:#93A0B1!important}
</style>
"""


def _legacy_report_html(text: str) -> str:
    """给旧版归档报告增加深色显示层，不改动磁盘原文件。"""
    if "</head>" in text:
        return text.replace("</head>", _LEGACY_REPORT_CSS + "</head>", 1)
    # 早期报告只是 HTML 片段，没有 head；末尾 style 仍会应用到整份文档。
    return text + _LEGACY_REPORT_CSS


def page():
    sel_g, sel_name, sel_label = current_sel()
    page_header("回测", f"{sel_label} · 使用已保存的策略配置重放历史并归档报告",
                eyebrow="Backtest lab")
    workflow_bar("backtest")
    sel_run = sel_g
    c1, c2 = st.columns(2)
    start = c1.text_input("开始", "2015-01-05")
    end = c2.text_input("结束", pd.Timestamp.today().strftime("%Y-%m-%d"))
    st.caption("费率/滑点等品种规则在 config/rules.yaml;止盈止损属策略语义,在『策略配置』页调整。"
               "结束日超出数据范围会自动截到最后一个交易日;缺行情的标的会先自动补数。")
    if yaml.safe_load(sel_run.read_text(encoding="utf-8")).get("strategy") == "dca":
        st.caption("定投策略:回测中计划起始日以上面的『开始』为准;"
                   "配置里的 start 只锚定影子/实盘的真实计划。")

    report_sel_key = f"bt_report_sel_{sel_name}"
    if st.button("运行回测", type="primary"):
        from sinan.backtest.engine import run_backtest
        from sinan.backtest.report import compute_stats, render_html
        from sinan.data.ensure import ensure_bars
        cfg = load_strategy(sel_run)
        cfg_snap = sel_run.read_text(encoding="utf-8")   # 回测时的配置快照
        settings_ = load_settings()
        # 缺行情的标的先自动补数(新浪源全量),拉不到才报错终止
        fetch_logs: list[str] = []
        with st.spinner("检查行情覆盖,缺失标的自动补数…"):
            still_missing = ensure_bars(get_store(), cfg.universe, cfg.sec_type,
                                        log=fetch_logs.append)
        for m in fetch_logs:
            st.info(m)
        if still_missing:
            st.error(f"以下标的无法获取行情,回测未运行:{'、'.join(still_missing)}"
                     "——请检查代码是否正确,或先经 bootstrap/影子更新入库。")
        else:
            with st.spinner("逐日回测运行中(池越大越慢,约 1~5 分钟)…"):
                res = run_backtest(get_store(), cfg, settings_, start=start, end=end,
                                   initial_capital=cfg.capital or settings_.capital)
                stats = compute_stats(res)
                out = settings_.reports_dir / f"{cfg.name}_{start}_{end}.html"
                render_html(res, stats, out)                       # 归档/导出件
                out.with_suffix(".cfg.yaml").write_text(cfg_snap, encoding="utf-8")
                save_result_json(res, stats, result_sidecar(out))  # 页面渲染数据源
            st.session_state.pop(report_sel_key, None)  # 让下方自动选中新报告
            st.success(f"回测完成,报告已存档:{out.name}(见下方)")

    st.divider()
    section_title(f"回测报告 · {sel_label}")
    reports = sorted((p for p in Path(load_settings().reports_dir).glob("*.html")
                      if p.name.startswith(sel_name + "_")),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        st.info(f"策略 {sel_label} 暂无回测报告,先在上方运行一次")
        return
    st.caption("每次回测自动存档为一份报告;默认展示最新一份,可切换历史版本对比。")
    r1, r2 = st.columns([4, 1])
    rp = r1.selectbox("选择报告(默认最新)", reports,
                      format_func=lambda p: p.name, key=report_sel_key)
    side = rp.with_suffix(".cfg.yaml")
    r2.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
    if r2.button("删除", key="rp_del"):
        confirm_delete(rp, extras=[side, result_sidecar(rp)], label="报告",
                       reset_key=report_sel_key)
    with st.expander("该报告的策略配置快照 vs 当前配置"):
        if side.exists():
            snap_txt = side.read_text(encoding="utf-8")
            cur_txt = sel_g.read_text(encoding="utf-8")
            diff = list(difflib.unified_diff(
                snap_txt.splitlines(), cur_txt.splitlines(),
                fromfile="回测时", tofile="当前", lineterm=""))
            if diff:
                st.markdown("**差异(− 回测时 / + 当前)**——想回滚某个参数,"
                            "按 − 行的值改回『策略配置』页即可")
                st.code("\n".join(diff), language="diff")
            else:
                st.success("与当前配置一致,无差异")
            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("**回测时配置**")
                st.code(snap_txt, language="yaml")
            with cc2:
                st.markdown("**当前配置**")
                st.code(cur_txt, language="yaml")
        else:
            st.caption("该报告没有配置快照(快照随本次功能引入,此后新跑的报告才有)。")
    if result_sidecar(rp).exists():
        show_report(rp)
        with st.expander("HTML 版报告(邮件/归档用,内容与上方一致)"):
            st.download_button("下载 HTML", rp.read_bytes(),
                               file_name=rp.name, mime="text/html",
                               key=f"dl_{rp.stem}")
    else:
        st.caption("旧版报告(无结构化数据,以下为归档 HTML 原文):")
        st.components.v1.html(_legacy_report_html(rp.read_text(encoding="utf-8")),
                              height=900, scrolling=True)
