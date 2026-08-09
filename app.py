"""
司南(SiNan)· 本地操作面板入口(Streamlit 多页架构)

    cd sinan && streamlit run app.py

左侧导航(st.navigation,页面相互隔离、按需执行):
  量化策略:策略看板(影子/实盘)/ 回测 / 策略配置
  数据中心:行情查询 / 数据仓概况 / 数据更新
  系统:    设置
页面右上角常驻"当前策略(全局)"选择器,贯穿所有页面。
页面实现在 ui/ 包(共享层 ui/common.py);本文件只做路由与全局头部,
不含任何策略/引擎逻辑。
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="司南 · 量化仓位导航", page_icon="🧭", layout="wide")

from ui import (backtest, quotes, settings_page, shadow, strategy_config,  # noqa: E402
                updater, warehouse)
from ui.common import cfg_label, strategy_files                            # noqa: E402

nav = st.navigation({
    "量化策略": [
        st.Page(shadow.page, title="策略看板", icon="📊", url_path="shadow",
                default=True),
        st.Page(backtest.page, title="回测", icon="🧪", url_path="backtest"),
        st.Page(strategy_config.page, title="策略配置", icon="⚙️", url_path="config"),
    ],
    "数据中心": [
        st.Page(quotes.page, title="行情查询", icon="📈", url_path="quotes"),
        st.Page(warehouse.page, title="数据仓概况", icon="🗄️", url_path="warehouse"),
        st.Page(updater.page, title="数据更新", icon="🔄", url_path="update"),
    ],
    "系统": [
        st.Page(settings_page.page, title="设置", icon="🛠️", url_path="settings"),
    ],
})

# 页面头部:标题在左,全局策略选择器常驻右上(贯穿所有页面,key=global_cfg)
cfgs = strategy_files()
default_ix = next((i for i, f in enumerate(cfgs)
                   if f.stem == "combo_turtle_xsmom_x2"), 0)
h_left, h_right = st.columns([2.4, 1], vertical_alignment="bottom")
h_left.title("🧭 司南 · 量化仓位导航")
with h_right:
    sel_g = st.selectbox("当前策略(全局)", cfgs, index=default_ix,
                         format_func=cfg_label, key="global_cfg")
    st.caption(f"配置文件:{sel_g.name}")

nav.run()
