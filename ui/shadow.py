"""影子模式页:一键"拉数+质检+出目标仓位",查看最新与历史 targets。"""
import subprocess
import sys

import streamlit as st

from ui.common import (ROOT, confirm_delete, current_sel, show_targets,
                       targets_files)


def page():
    sel_g, sel_name, sel_label = current_sel()
    st.subheader("影子模式:更新数据 → 质检 → 生成目标仓位")
    st.caption(f"当前策略:**{sel_label}**(在左侧边栏切换)")
    if st.button("▶ 一键更新并生成 targets", type="primary"):
        with st.spinner("拉数 → 质检 → 出信号(约 1~2 分钟)…"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "shadow_update.py"),
                                "--strategy", str(sel_g)],
                               cwd=ROOT, capture_output=True, text=True)
        st.code((r.stdout + r.stderr)[-3000:] or "(无输出)")
        if r.returncode == 0:
            st.success("完成")
        else:
            st.error("失败——质检未过或数据源不可用,详见日志。未生成新 targets。")
    st.divider()
    tfs = targets_files(sel_name)
    if tfs:
        st.markdown(f"**最新目标仓位(策略:{sel_label})**")
        show_targets(tfs[0])
        with st.expander(f"历史 targets(策略:{sel_label})"):
            s1, s2 = st.columns([4, 1])
            tp = s1.selectbox("选择日期", tfs, format_func=lambda p: p.name)
            s2.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
            if s2.button("🗑 删除", key="tgt_del"):
                confirm_delete(tp, label="targets 文件")
            if tp != tfs[0]:
                show_targets(tp)
    else:
        st.info(f"策略 {sel_label} 尚无 targets 文件,先点上面的按钮生成一份。")
