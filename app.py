"""
司南(SiNan)· 本地操作面板入口(Streamlit 多页架构)

    cd sinan && streamlit run app.py

左侧导航(st.navigation,页面相互隔离、按需执行):
  量化策略:影子模式 / 回测 / 策略配置
  数据中心:行情查询 / 数据仓概况 / 数据更新
侧栏常驻"当前策略(全局)"选择器,贯穿所有页面。
页面实现在 ui/ 包(共享层 ui/common.py);本文件只做路由与全局侧栏,
不含任何策略/引擎逻辑。
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="司南 · 量化仓位导航", page_icon="🧭", layout="wide")

from ui import backtest, quotes, shadow, strategy_config, updater, warehouse  # noqa: E402
from ui.common import cfg_label, strategy_files                               # noqa: E402

nav = st.navigation({
    "量化策略": [
        st.Page(shadow.page, title="影子模式", icon="🛰️", url_path="shadow",
                default=True),
        st.Page(backtest.page, title="回测", icon="🧪", url_path="backtest"),
        st.Page(strategy_config.page, title="策略配置", icon="⚙️", url_path="config"),
    ],
    "数据中心": [
        st.Page(quotes.page, title="行情查询", icon="📈", url_path="quotes"),
        st.Page(warehouse.page, title="数据仓概况", icon="🗄️", url_path="warehouse"),
        st.Page(updater.page, title="数据更新", icon="🔄", url_path="update"),
    ],
})

# 常驻侧栏:全局策略选择器(所有页面共享,key=global_cfg)
cfgs = strategy_files()
default_ix = next((i for i, f in enumerate(cfgs)
                   if f.stem == "combo_turtle_xsmom_x2"), 0)
with st.sidebar:
    st.divider()
    sel_g = st.selectbox("当前策略(全局)", cfgs, index=default_ix,
                         format_func=cfg_label, key="global_cfg")
    st.caption(f"配置文件:{sel_g.name}\n\n此选择贯穿左侧所有页面")

st.title("🧭 司南 · 量化仓位导航")
nav.run()
