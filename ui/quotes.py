"""行情查询页:标的搜索 → K 线/区间行情 → 原始数据表(可下载 CSV)。"""
import pandas as pd
import streamlit as st

from ui.common import NAV_COLOR, etf_names, get_store, instruments_df


def page():
    st.subheader("行情查询")
    st.caption("parquet + DuckDB 数据仓;后复权=信号口径,原始价=执行口径。")
    q = st.text_input("搜索标的(代码或名称片段)", "510300")
    inst = instruments_df()
    hits = inst[inst["symbol"].str.contains(q, na=False)
                | inst["name"].str.contains(q, na=False)] if q else inst.head(0)
    if len(hits) == 0:
        st.info("无匹配标的")
        return
    pick = st.selectbox("匹配结果", hits["symbol"].tolist(),
                        format_func=lambda s: f"{s} {etf_names().get(s, '')}")
    c1, c2, c3 = st.columns(3)
    d_start = c1.text_input("开始日期", "2024-01-01")
    d_end = c2.text_input("结束日期", pd.Timestamp.today().strftime("%Y-%m-%d"))
    adj = c3.toggle("后复权", value=True,
                    help="开=原始价×复权因子(信号口径);关=交易所原始价(执行口径)")
    bars = get_store().read_bars(symbols=[pick], sec_type="etf",
                                 start=d_start, end=d_end, adjust=adj)
    if bars.empty:
        st.warning("区间内无数据")
        return
    bars = bars.sort_values("date")
    full = get_store().read_bars(symbols=[pick], sec_type="etf")
    m = st.columns(5)
    m[0].metric("区间行数", len(bars))
    m[1].metric("数据覆盖", f"{full['date'].min().date()} 起")
    m[2].metric("最新日期", str(full["date"].max().date()))
    m[3].metric("最新收盘", f"{bars['close'].iloc[-1]:.3f}")
    fac = full.get("adj_factor")
    m[4].metric("最新复权因子",
                f"{float(fac.iloc[-1]):.4f}" if fac is not None and len(fac) else "—")
    try:
        import plotly.graph_objects as go
        fig = go.Figure(go.Candlestick(
            x=bars["date"], open=bars["open"], high=bars["high"],
            low=bars["low"], close=bars["close"],
            increasing_line_color="#C4433F", decreasing_line_color="#2E8B57"))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                          xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, width="stretch")
    except ImportError:
        st.line_chart(bars.set_index("date")["close"].rename("收盘"),
                      color=NAV_COLOR, height=360)
    with st.expander("原始数据表(工具栏可下载 CSV)"):
        st.dataframe(bars.reset_index(drop=True), width="stretch", height=380)
