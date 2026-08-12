"""
司南(SiNan)· 本地量化研究终端入口(Streamlit 多页架构)

    cd sinan && streamlit run app.py

左侧导航(st.navigation,页面相互隔离、按需执行):
  量化策略:策略看板(影子/实盘)/ 回测 / 策略配置 / 设置
  数据中心:行情查询 / 数据仓概况 / 数据更新
"当前策略(全局)"选择器只在量化策略模块的页面右上角出现(数据中心与
策略无关),切换页面时选择保持。页面实现在 ui/ 包;本文件只做路由与
全局头部,不含任何策略/引擎逻辑。
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOGO_PATH = ROOT / "assets" / "ui" / "sinan-compass.png"
BRAND_PATH = ROOT / "assets" / "ui" / "sinan-wordmark.png"
st.set_page_config(page_title="司南 · 量化平台",
                   page_icon=str(LOGO_PATH), layout="wide")

from ui import (backtest, quotes, settings_page, shadow, strategy_config,  # noqa: E402
                updater, warehouse)
from ui.common import cfg_label, strategy_files                            # noqa: E402
from ui.theme import apply_theme                                           # noqa: E402

apply_theme()
st.logo(str(BRAND_PATH), size="large", icon_image=str(LOGO_PATH))

pg = st.navigation({
    "数据": [
        st.Page(quotes.page, title="行情查询", icon=":material/candlestick_chart:", url_path="quotes"),
        st.Page(warehouse.page, title="数据仓概况", icon=":material/database:", url_path="warehouse"),
        st.Page(updater.page, title="数据更新", icon=":material/sync:", url_path="update"),
    ],
    "回测与实盘": [
        st.Page(shadow.page, title="策略看板", icon=":material/monitoring:", url_path="shadow",
                default=True),
        st.Page(backtest.page, title="回测", icon=":material/science:", url_path="backtest"),
    ],
    "策略与设置": [
        st.Page(strategy_config.page, title="策略配置", icon=":material/tune:", url_path="config"),
        st.Page(settings_page.page, title="设置", icon=":material/settings:", url_path="settings"),
    ],
})

_QUANT_PAGES = {"", "shadow", "backtest", "config"}  # 默认页 url_path 为空

if getattr(pg, "url_path", "").strip("/") in _QUANT_PAGES:
    # 紧凑策略上下文栏；选择跨页持久(_sel_path)。
    cfgs = strategy_files()
    prev = st.session_state.get("_sel_path")
    default_ix = (cfgs.index(prev) if prev in cfgs else
                  next((i for i, f in enumerate(cfgs)
                        if f.stem == "combo_turtle_xsmom_x2"), 0))
    with st.container(key="strategy_context"):
        h_label, h_strategy, h_id = st.columns([.7, 2.2, 1.15],
                                                vertical_alignment="bottom")
        h_label.markdown('<div class="sn-context-label">当前策略 · Global</div>',
                         unsafe_allow_html=True)
        with h_strategy:
            sel_g = st.selectbox("当前策略(全局)", cfgs, index=default_ix,
                                 format_func=cfg_label, key="global_cfg",
                                 label_visibility="collapsed")
            st.session_state["_sel_path"] = sel_g
            import yaml as _yaml
            _sid = (_yaml.safe_load(sel_g.read_text(encoding="utf-8"))
                    or {}).get("name", sel_g.stem)
        h_id.markdown(
            '<div class="sn-context-label">策略 ID</div>'
            f'<div class="sn-context-id">{_sid}</div>',
            unsafe_allow_html=True,
        )

pg.run()
