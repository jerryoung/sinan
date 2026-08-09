"""数据更新页:增量更新当前策略池(新浪+质检门)、手动补数工具。"""
import subprocess
import sys

import streamlit as st

from ui.common import ROOT, current_sel, get_store


def page():
    sel_g, _, sel_label = current_sel()
    st.subheader("数据更新")

    # 当前策略池的数据新鲜度
    import yaml
    d = yaml.safe_load(sel_g.read_text(encoding="utf-8")) or {}
    uni = [str(s) for s in d.get("universe", [])]
    sec = d.get("sec_type", "etf")
    bars = get_store().read_bars(symbols=uni, sec_type=sec)
    m = st.columns(3)
    m[0].metric("当前策略", sel_label)
    m[1].metric("池内标的", f"{len(uni)} 只")
    m[2].metric("数据最新日期", str(bars["date"].max().date()) if len(bars) else "无数据")

    st.markdown("**增量更新**(新浪端点逐只拉取 → 质检门 → 入库;不生成信号)")
    if st.button("▶ 增量更新当前策略池", type="primary"):
        with st.spinner("拉取 + 质检 + 入库…"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "shadow_update.py"),
                                "--strategy", str(sel_g), "--skip-signal"],
                               cwd=ROOT, capture_output=True, text=True)
        st.code((r.stdout + r.stderr)[-3000:] or "(无输出)")
        if r.returncode == 0:
            st.cache_data.clear()          # 覆盖概况/行情缓存立即失效
            st.success("更新完成")
        else:
            st.error("更新失败——质检未过或数据源不可用,详见日志。")
    st.caption("要顺带生成目标仓位,请用『影子模式』页的一键链路。")

    st.divider()
    st.markdown("**手动补数**(标的在 store 无行情时拉全量入库;仅支持 ETF)")
    codes = st.text_input("标的代码(逗号/空格分隔)", "", key="upd_codes",
                          placeholder="例:588000 510880")
    if st.button("拉取并入库") and codes.strip():
        from sinan.data.ensure import ensure_bars
        syms = [s.strip() for s in codes.replace(",", " ").split() if s.strip()]
        logs: list[str] = []
        with st.spinner("补数中…"):
            still = ensure_bars(get_store(), syms, "etf", log=logs.append)
        for msg in logs:
            st.info(msg)
        if still:
            st.error(f"以下标的未能获取:{'、'.join(still)}")
        else:
            st.cache_data.clear()
            st.success("补数完成")
    st.caption("全市场种子化/重建请在命令行运行 scripts/bootstrap_from_csv.py;"
               "tushare 主源恢复权限后将提供双源对账。")
