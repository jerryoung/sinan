"""司南 Streamlit 设计系统：主题、页头、流程导航与状态键值。"""
from __future__ import annotations

from html import escape

import streamlit as st


_CSS = r"""
<style>
:root {
  --sn-canvas: #0B1118;
  --sn-sidebar: #0F1722;
  --sn-surface: #111A25;
  --sn-surface-raised: #151F2C;
  --sn-border: #273241;
  --sn-border-soft: rgba(147, 160, 177, .16);
  --sn-text: #E8EDF4;
  --sn-text-muted: #93A0B1;
  --sn-primary: #5B7CFA;
  --sn-primary-soft: #263965;
  --sn-success: #3DBE8B;
  --sn-warning: #E4A853;
  --sn-danger: #F05D64;
  --sn-radius: 6px;
  --sn-font: "Source Sans 3", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  --sn-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

html, body, [class*="css"], [data-testid="stAppViewContainer"] {
  font-family: var(--sn-font);
}

.stApp, [data-testid="stAppViewContainer"] {
  background: var(--sn-canvas);
  color: var(--sn-text);
}

[data-testid="stHeader"] {
  height: 3rem;
  background: transparent;
  pointer-events: none;
}

[data-testid="stHeader"] button,
[data-testid="stExpandSidebarButton"] {
  pointer-events: auto;
}

[data-testid="stToolbar"] {
  background: transparent;
  pointer-events: none;
}

[data-testid="stHeaderLogo"] {
  display: none !important;
}

[data-testid="stExpandSidebarButton"] {
  position: fixed;
  top: .65rem;
  left: .75rem;
  z-index: 1000;
  color: var(--sn-text);
  background: var(--sn-surface-raised);
  border: 1px solid var(--sn-border);
  border-radius: var(--sn-radius);
}

.stDeployButton, #MainMenu, footer {
  display: none !important;
}

[data-testid="stMainBlockContainer"] {
  max-width: none !important;
  padding: 1rem 1.5rem 3rem !important;
}

[data-testid="stSidebar"] {
  min-width: 208px;
  max-width: 208px;
  background: var(--sn-sidebar);
  border-right: 1px solid var(--sn-border);
}

[data-testid="stSidebarContent"] {
  padding-top: .8rem;
}

[data-testid="stSidebarLogo"] {
  height: 56px !important;
  width: 168px !important;
  margin: 0 !important;
  border-radius: 0;
  object-fit: contain;
  object-position: left center;
}

[data-testid="stSidebarHeader"] { min-height: 68px; }

[data-testid="stSidebarNav"] span,
[data-testid="stSidebarNav"] p {
  font-size: .88rem;
}

[data-testid="stSidebarNav"] a {
  border-radius: var(--sn-radius);
  color: #B4BFCD;
  min-height: 2.5rem;
}

[data-testid="stSidebarNav"] a:hover {
  color: var(--sn-text);
  background: rgba(91, 124, 250, .10);
}

[data-testid="stSidebarNav"] a[aria-current="page"] {
  color: #F5F7FB;
  background: var(--sn-primary-soft);
  box-shadow: inset 2px 0 0 var(--sn-primary);
}

h1, h2, h3, h4, h5, h6 {
  color: var(--sn-text);
  letter-spacing: -.015em;
}

p, label, [data-testid="stCaptionContainer"] {
  color: var(--sn-text-muted);
}

a { color: #89A2FF; }

.st-key-strategy_context {
  min-height: 58px !important;
  margin: 0 0 .6rem !important;
  padding: .2rem 0 .7rem !important;
  border-bottom: 1px solid var(--sn-border) !important;
}

.sn-context-label {
  color: var(--sn-text-muted);
  font-size: .72rem;
  line-height: 1.1;
  letter-spacing: .04em;
  text-transform: uppercase;
  margin: .12rem 0 .38rem;
}

.st-key-strategy_context .sn-context-label {
  min-height: 2.5rem;
  display: flex;
  align-items: center;
  margin: 0;
}

.sn-context-id {
  min-height: 2.5rem;
  display: flex;
  align-items: center;
  padding: 0 .8rem;
  color: #BFC9D8;
  background: var(--sn-surface);
  border: 1px solid var(--sn-border);
  border-radius: var(--sn-radius);
  font-family: var(--sn-mono);
  font-size: .82rem;
  overflow-wrap: anywhere;
}

.sn-page-header {
  margin: .35rem 0 .95rem;
}

.sn-page-header__eyebrow {
  color: var(--sn-primary);
  font-size: .72rem;
  font-weight: 650;
  letter-spacing: .1em;
  text-transform: uppercase;
  margin-bottom: .22rem;
}

.sn-page-header__title {
  color: var(--sn-text);
  font-size: 1.55rem;
  font-weight: 680;
  line-height: 1.25;
  letter-spacing: -.02em;
}

.sn-page-header__description {
  color: var(--sn-text-muted);
  max-width: 78ch;
  font-size: .88rem;
  margin-top: .35rem;
  line-height: 1.55;
}

.sn-workflow {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  min-height: 48px;
  margin: 0 0 1rem;
  border: 1px solid var(--sn-border);
  border-radius: var(--sn-radius);
  overflow: hidden;
  background: var(--sn-surface);
}

.sn-workflow__step {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: .55rem;
  color: #9CA9BA;
  font-size: .88rem;
  border-right: 1px solid var(--sn-border);
}

.sn-workflow__step:last-child { border-right: 0; }

.sn-workflow__step.is-active {
  color: #F3F6FB;
  background: var(--sn-primary-soft);
  box-shadow: inset 0 -2px 0 var(--sn-primary);
}

.sn-workflow__index {
  color: inherit;
  font-family: var(--sn-mono);
  font-size: .72rem;
  opacity: .72;
}

.sn-workflow__en {
  color: inherit;
  font-size: .72rem;
  opacity: .66;
}

.sn-lifecycle {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  align-items: center;
  min-height: 70px;
  margin: 0 0 1rem;
}

.sn-lifecycle__item {
  position: relative;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: center;
  gap: .7rem;
  min-width: 0;
}

.sn-lifecycle__item:not(:last-child)::after {
  content: "";
  position: absolute;
  top: 17px;
  right: 1rem;
  width: clamp(28px, 6vw, 96px);
  height: 1px;
  background: var(--sn-border);
}

.sn-lifecycle__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  color: #FFFFFF;
  background: #1E743F;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.12);
}

.sn-lifecycle__icon .material-symbols-rounded {
  font-family: "Material Symbols Rounded";
  font-size: 18px;
  font-weight: normal;
  font-style: normal;
  line-height: 1;
  letter-spacing: normal;
  text-transform: none;
  white-space: nowrap;
  word-wrap: normal;
  direction: ltr;
  font-feature-settings: "liga";
  -webkit-font-feature-settings: "liga";
}
.sn-lifecycle__item.is-primary .sn-lifecycle__icon {
  color: #AFC0FF;
  background: #173D76;
  box-shadow: inset 0 0 0 1px #4B82E8, 0 0 12px rgba(75,130,232,.2);
}
.sn-lifecycle__title {
  color: var(--sn-text);
  font-size: .92rem;
  font-weight: 620;
}
.sn-lifecycle__detail {
  margin-top: .18rem;
  color: var(--sn-text-muted);
  font-size: .74rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sn-section-title {
  color: var(--sn-text);
  font-size: 1rem;
  font-weight: 650;
  margin: .15rem 0 .65rem;
}

.sn-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin: .2rem 0 .85rem;
  border-bottom: 1px solid var(--sn-border-soft);
}

.sn-metric {
  min-width: 0;
  padding: .15rem .8rem .7rem;
  border-right: 1px solid var(--sn-border-soft);
}

.sn-metric:first-child { padding-left: 0; }
.sn-metric:last-child { border-right: 0; }
.sn-metric__label {
  min-height: 1.15rem;
  color: var(--sn-text-muted);
  font-size: .72rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sn-metric__value {
  color: var(--sn-text);
  font-size: clamp(1.05rem, 1.4vw, 1.55rem);
  font-weight: 520;
  line-height: 1.35;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sn-metric__value.is-danger { color: var(--sn-danger); }
.sn-metric__value.is-success { color: var(--sn-success); }
.sn-metric__value.is-primary { color: #90A7FF; }

.sn-kv {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: .34rem 0;
  border-bottom: 1px solid var(--sn-border-soft);
  font-size: .82rem;
}

.sn-kv:last-child { border-bottom: 0; }
.sn-kv__label { color: var(--sn-text-muted); }
.sn-kv__value { color: var(--sn-text); text-align: right; font-variant-numeric: tabular-nums; }
.sn-kv__value.is-success { color: var(--sn-success); }
.sn-kv__value.is-warning { color: var(--sn-warning); }
.sn-kv__value.is-danger { color: var(--sn-danger); }
.sn-kv__value.is-primary { color: #90A7FF; }

div[data-testid="stVerticalBlockBorderWrapper"] {
  background: rgba(17, 26, 37, .72);
  border-color: var(--sn-border) !important;
  border-radius: var(--sn-radius) !important;
}

div[data-testid="stMetric"] {
  padding: .2rem .75rem .35rem 0;
  border-right: 1px solid var(--sn-border-soft);
}

div[data-testid="column"]:last-child div[data-testid="stMetric"] {
  border-right: 0;
}

[data-testid="stMetricLabel"] p {
  color: var(--sn-text-muted);
  font-size: .76rem;
}

[data-testid="stMetricValue"] {
  color: var(--sn-text);
  font-variant-numeric: tabular-nums;
}

[data-testid="stMetricDelta"] { font-size: .74rem; }

.stButton > button,
.stDownloadButton > button {
  min-height: 2.45rem;
  border-radius: var(--sn-radius);
  border-color: #354255;
  background: transparent;
  color: #D7DEE8;
  font-weight: 560;
}

.stButton button p,
.stDownloadButton button p {
  color: inherit;
}

.stButton > button:hover,
.stDownloadButton > button:hover {
  border-color: #6E8BFF;
  color: #FFFFFF;
  background: rgba(91, 124, 250, .10);
}

.stButton > button[kind="primary"] {
  color: #FFFFFF;
  border-color: var(--sn-primary);
  background: var(--sn-primary);
}

.stButton > button[kind="primary"]:hover {
  border-color: #7894FF;
  background: #6B88FC;
}

/* 方案 1 使用单一蓝色主操作，避免把正常更新误读为危险动作。 */
.st-key-shadow_update_targets button[kind="primary"] {
  border-color: var(--sn-primary);
  background: var(--sn-primary);
}

.st-key-shadow_update_targets button[kind="primary"]:hover {
  border-color: #7894FF;
  background: #6B88FC;
}

button:focus-visible, input:focus-visible, textarea:focus-visible,
[role="combobox"]:focus-visible, [role="tab"]:focus-visible {
  outline: 2px solid #86A0FF !important;
  outline-offset: 2px;
}

[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] > div {
  min-height: 2.5rem;
  color: var(--sn-text);
  background: var(--sn-surface) !important;
  border-color: var(--sn-border) !important;
  border-radius: var(--sn-radius) !important;
}

[data-baseweb="tab-list"] {
  gap: .25rem;
  border-bottom: 1px solid var(--sn-border);
}

[data-baseweb="tab"] {
  color: var(--sn-text-muted);
  padding-left: .75rem;
  padding-right: .75rem;
}

[aria-selected="true"][role="tab"] {
  color: var(--sn-text);
}

[data-testid="stDataFrame"], [data-testid="stTable"] {
  border: 1px solid var(--sn-border);
  border-radius: var(--sn-radius);
  overflow: hidden;
}

[data-testid="stExpander"] {
  border-color: var(--sn-border) !important;
  border-radius: var(--sn-radius) !important;
  background: transparent;
}

[data-testid="stAlert"] {
  border-radius: var(--sn-radius);
  border-width: 1px;
}

hr {
  border-color: var(--sn-border) !important;
}

code, pre {
  font-family: var(--sn-mono) !important;
}

@media (max-width: 1100px) {
  [data-testid="stSidebar"] { min-width: 184px; max-width: 184px; }
  [data-testid="stMainBlockContainer"] {
    padding-left: 1rem !important;
    padding-right: 1rem !important;
  }
  .sn-context-id { font-size: .72rem; }
  .sn-metric__value { font-size: 1.28rem; }
}

@media (max-width: 760px) {
  .sn-workflow__en { display: none; }
  .sn-page-header__title { font-size: 1.3rem; }
  .sn-lifecycle { grid-template-columns: 1fr; gap: .65rem; }
  .sn-lifecycle__item:not(:last-child)::after { display: none; }
}
</style>
"""


def apply_theme() -> None:
    """注入全平台共享样式；只改变显示，不承载业务逻辑。"""
    st.markdown(_CSS, unsafe_allow_html=True)


def page_header(title: str, description: str = "", *, eyebrow: str = "") -> None:
    """统一页面标题与简短说明。"""
    eyebrow_html = (
        f'<div class="sn-page-header__eyebrow">{escape(eyebrow)}</div>'
        if eyebrow else ""
    )
    description_html = (
        f'<div class="sn-page-header__description">{escape(description)}</div>'
        if description else ""
    )
    st.markdown(
        '<div class="sn-page-header">'
        f'{eyebrow_html}<div class="sn-page-header__title">{escape(title)}</div>'
        f'{description_html}</div>',
        unsafe_allow_html=True,
    )


def workflow_bar(active: str) -> None:
    """展示数据、回测、实盘三段核心流程；active 为 data/backtest/live。"""
    steps = (
        ("data", "01", "数据", "Data"),
        ("backtest", "02", "回测", "Backtest"),
        ("live", "03", "实盘", "Live"),
    )
    items = []
    for key, index, label, english in steps:
        active_class = " is-active" if key == active else ""
        items.append(
            f'<div class="sn-workflow__step{active_class}">'
            f'<span class="sn-workflow__index">{index}</span>'
            f'<span>{label}</span><span class="sn-workflow__en">{english}</span>'
            "</div>"
        )
    st.markdown('<div class="sn-workflow">' + "".join(items) + "</div>",
                unsafe_allow_html=True)


def lifecycle_status(items: list[tuple[str, str, str]]) -> None:
    """方案 1 的核心运行链：策略更新、回测验证、影子/实盘运行。"""
    cells = []
    for title, detail, tone in items:
        tone_class = " is-primary" if tone == "primary" else ""
        icon = "radio_button_checked" if tone == "primary" else "check"
        cells.append(
            f'<div class="sn-lifecycle__item{tone_class}">'
            '<div class="sn-lifecycle__icon">'
            f'<span class="material-symbols-rounded">{icon}</span></div>'
            '<div><div class="sn-lifecycle__title">'
            f'{escape(title)}</div><div class="sn-lifecycle__detail">'
            f'{escape(detail)}</div></div></div>'
        )
    st.markdown('<div class="sn-lifecycle">' + "".join(cells) + "</div>",
                unsafe_allow_html=True)


def section_title(text: str) -> None:
    """容器内的轻量分区标题。"""
    st.markdown(f'<div class="sn-section-title">{escape(text)}</div>',
                unsafe_allow_html=True)


def metric_strip(items: list[tuple[str, object, str]]) -> None:
    """连续指标带；items 为 (label, value, tone)。"""
    cells = []
    for label, value, tone in items:
        tone_class = f" is-{tone}" if tone in {"danger", "success", "primary"} else ""
        cells.append(
            '<div class="sn-metric">'
            f'<div class="sn-metric__label">{escape(str(label))}</div>'
            f'<div class="sn-metric__value{tone_class}">{escape(str(value))}</div>'
            '</div>'
        )
    columns = max(len(items), 1)
    st.markdown(
        f'<div class="sn-metrics" style="grid-template-columns:'
        f'repeat({columns}, minmax(0, 1fr))">' + "".join(cells) + "</div>",
        unsafe_allow_html=True,
    )


def status_kv(label: str, value: object, *, tone: str = "") -> None:
    """右侧决策栏使用的真实状态键值。"""
    tone_class = f" is-{tone}" if tone in {"success", "warning", "danger", "primary"} else ""
    st.markdown(
        '<div class="sn-kv">'
        f'<span class="sn-kv__label">{escape(str(label))}</span>'
        f'<span class="sn-kv__value{tone_class}">{escape(str(value))}</span>'
        "</div>",
        unsafe_allow_html=True,
    )
