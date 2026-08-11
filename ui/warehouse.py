"""数据仓概况页:三品种覆盖、交易日历、标的档案与磁盘占用。"""
import pandas as pd
import streamlit as st

from sinan.config import load_settings
from ui.common import get_store
from ui.theme import metric_strip, page_header, section_title, workflow_bar

_SEC_LABEL = {"etf": "ETF", "stock": "个股", "cb": "转债"}


@st.cache_data(ttl=600)
def _coverage() -> pd.DataFrame:
    return get_store().coverage()


@st.cache_data(ttl=3600)
def _disk_mb() -> float:
    root = load_settings().store_root
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1e6


def page():
    page_header("数据仓概况", "检查品种覆盖、交易日历与本地存储健康度",
                eyebrow="Data warehouse")
    workflow_bar("data")
    store = get_store()
    cov = _coverage()
    if cov.empty:
        st.warning("数据仓为空——先运行 scripts/bootstrap_from_csv.py 种子化。")
        return
    cal = store.read_calendar()
    metric_strip([
        ("品种", " / ".join(_SEC_LABEL.get(t, t) for t in cov["sec_type"]), ""),
        ("总行数", f"{int(cov['n_rows'].sum()):,}", ""),
        ("交易日历", f"{cal[0].date()} → {cal[-1].date()}", ""),
        ("磁盘占用", f"{_disk_mb():,.0f} MB", ""),
    ])

    section_title("分品种覆盖")
    view = cov.copy()
    view["sec_type"] = view["sec_type"].map(lambda t: _SEC_LABEL.get(t, t))
    view["min_date"] = view["min_date"].dt.date
    view["max_date"] = view["max_date"].dt.date
    view.columns = ["品种", "标的数", "行数", "最早日期", "最新日期"]
    view["行数"] = view["行数"].map(lambda x: f"{int(x):,}")
    st.dataframe(view, width="stretch", hide_index=True)
    st.caption("最新日期滞后于交易日历末端属正常:增量更新只覆盖影子策略池,"
               "全市场补全走 scripts/daily_update.py 或重新 bootstrap。")

    inst = store.read_instruments()
    if len(inst):
        with st.expander(f"标的档案(instruments,共 {len(inst)} 条)"):
            kw = st.text_input("筛选(代码/名称)", "", key="wh_inst_kw")
            v = inst.astype(str)
            if kw:
                v = v[v["symbol"].str.contains(kw, na=False)
                      | v.get("name", pd.Series(dtype=str)).str.contains(kw, na=False)]
            st.dataframe(v.head(500), width="stretch", height=360)
