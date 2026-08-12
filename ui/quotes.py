"""行情查询页:标的搜索 → K 线/区间行情 → 原始数据表(可下载 CSV)。"""
import pandas as pd
import streamlit as st

from ui.common import NAV_COLOR, get_store
from ui.theme import metric_strip, page_header, workflow_bar

_SEC_LABEL = {"etf": "ETF", "stock": "个股", "cb": "转债"}


def market_catalog(store) -> pd.DataFrame:
    """合并真实行情目录与标的主数据;缺主数据也允许按代码查询。"""
    bars = store.list_bar_symbols()
    if bars.empty:
        return pd.DataFrame(columns=["symbol", "sec_type", "name", "n_rows",
                                     "min_date", "max_date"])
    inst = store.read_instruments()
    names = (inst[["symbol", "name"]].assign(
        symbol=lambda x: x["symbol"].astype(str)
    ).drop_duplicates("symbol", keep="last") if len(inst)
             and {"symbol", "name"} <= set(inst.columns)
             else pd.DataFrame(columns=["symbol", "name"]))
    out = bars.merge(names, on="symbol", how="left")
    out["name"] = out["name"].fillna("").astype(str)
    return out


def page():
    page_header("行情查询", "从本地数据仓检查信号口径与执行口径的行情覆盖",
                eyebrow="Market data")
    workflow_bar("data")
    q = st.text_input("搜索标的(代码或名称片段)", "510300")
    catalog = market_catalog(get_store())
    hits = catalog[catalog["symbol"].str.contains(q, na=False)
                   | catalog["name"].str.contains(q, na=False)] if q else catalog.head(0)
    if len(hits) == 0:
        st.info("无匹配标的")
        return
    options = list(hits[["symbol", "sec_type"]].itertuples(index=False, name=None))
    labels = {(row.symbol, row.sec_type):
              f"{row.symbol} {row['name']} · {_SEC_LABEL.get(row.sec_type, row.sec_type)}"
              for _, row in hits.iterrows()}
    pick, sec_type = st.selectbox("匹配结果", options,
                                  format_func=lambda item: labels[item])
    c1, c2, c3 = st.columns(3)
    d_start = c1.text_input("开始日期", "2024-01-01")
    d_end = c2.text_input("结束日期", pd.Timestamp.today().strftime("%Y-%m-%d"))
    adj = c3.toggle("后复权", value=True,
                    help="开=原始价×复权因子(信号口径);关=交易所原始价(执行口径)")
    bars = get_store().read_bars(symbols=[pick], sec_type=sec_type,
                                 start=d_start, end=d_end, adjust=adj)
    if bars.empty:
        st.warning("区间内无数据")
        return
    bars = bars.sort_values("date")
    full = get_store().read_bars(symbols=[pick], sec_type=sec_type)
    fac = full.get("adj_factor")
    metric_strip([
        ("区间行数", f"{len(bars):,}", ""),
        ("数据覆盖", f"{full['date'].min().date()} 起", ""),
        ("最新日期", str(full["date"].max().date()), ""),
        ("最新收盘", f"{bars['close'].iloc[-1]:.3f}", ""),
        ("最新复权因子",
         f"{float(fac.iloc[-1]):.4f}" if fac is not None and len(fac) else "—", ""),
    ])
    try:
        import plotly.graph_objects as go
        fig = go.Figure(go.Candlestick(
            x=bars["date"], open=bars["open"], high=bars["high"],
            low=bars["low"], close=bars["close"],
            increasing_line_color="#F05D64", decreasing_line_color="#3DBE8B"))
        fig.update_layout(
            height=420,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_rangeslider_visible=False,
            paper_bgcolor="#0B1118",
            plot_bgcolor="#0B1118",
            font=dict(color="#B8C2D0"),
            xaxis=dict(gridcolor="#273241", zerolinecolor="#273241"),
            yaxis=dict(gridcolor="#273241", zerolinecolor="#273241"),
        )
        st.plotly_chart(fig, width="stretch")
    except ImportError:
        st.line_chart(bars.set_index("date")["close"].rename("收盘"),
                      color=NAV_COLOR, height=360)
    with st.expander("原始数据表(工具栏可下载 CSV)"):
        st.dataframe(bars.reset_index(drop=True), width="stretch", height=380)
