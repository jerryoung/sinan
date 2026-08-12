"""设置页:系统设置与可复用实盘配置。

系统设置编辑 config/settings.yaml;实盘配置编辑 config/live_profiles.yaml。
策略级参数在『策略配置』页。控件 key 绑定文件内容指纹,防止外部改动后
旧控件状态在保存时回写。
"""
import hashlib

import streamlit as st
import yaml

from sinan.config import ExecutionCfg, RiskCfg, Settings
from ui.common import ROOT, enum_ix, num_or
from ui.live_profiles import render_live_profiles_page
from ui.theme import page_header, section_title

SET_PATH = ROOT / "config" / "settings.yaml"

# 表单缺省一律取自 pydantic 模型,不在界面上另写一份字面量:两份默认值
# 迟早漂移(max_positions 曾表单 12 / 模型 0,配置缺该键时面板显示 12,
# 一次保存就把"不限"静默改成 12)。
_DEF = Settings().model_dump()
_DEF_EXEC = ExecutionCfg().model_dump()
_DEF_RISK = RiskCfg().model_dump()


def page():
    page_header("设置", "系统能力与命名实盘配置的统一入口",
                eyebrow="Platform settings")
    system_tab, live_tab = st.tabs(["系统设置", "实盘配置"])
    with system_tab:
        _render_system_settings()
    with live_tab:
        render_live_profiles_page()


def _render_system_settings():
    st.caption("全局配置(config/settings.yaml):本金、执行、风控、数据源、通知。"
               "路径类配置建议保持默认,如需修改用高级模式。")
    raw = SET_PATH.read_text(encoding="utf-8")
    d = yaml.safe_load(raw) or {}
    fkey = hashlib.md5(raw.encode()).hexdigest()[:8]
    mode = st.radio("配置方式", ["表单", "高级(YAML)"], horizontal=True,
                    key="set_mode")

    if mode == "表单":
        out = dict(d)
        c1, c2 = st.columns(2)
        out["capital"] = float(c1.number_input(
            "capital(全局名义本金,元)", value=float(d.get("capital", _DEF["capital"])),
            step=100000.0, format="%.0f", key=f"set_{fkey}_cap",
            help="策略 YAML 未配置专属本金时的缺省值;影子参考委托单与回测初始资金"))
        out["wecom_webhook"] = c2.text_input(
            "企业微信 webhook(空=只写本地日志)", d.get("wecom_webhook", _DEF["wecom_webhook"]),
            type="password", key=f"set_{fkey}_hook",
            help="群机器人 webhook 地址;targets 生成与风控裁剪会推送摘要")

        section_title("执行（execution）")
        ex = dict(d.get("execution") or {})
        e1, e2, e3, e4 = st.columns(4)
        pm_opts = ["close", "open"]
        ex["price_mode"] = e1.selectbox(
            "price_mode(回测执行价)", pm_opts,
            index=enum_ix(pm_opts, ex.get("price_mode"), _DEF_EXEC["price_mode"]),
            key=f"set_{fkey}_pm", help="close 与实盘 14:45 执行近似对齐")
        ex["rebalance_band"] = float(e2.number_input(
            "rebalance_band(再平衡带宽)", value=num_or(ex.get("rebalance_band"), _DEF_EXEC["rebalance_band"], float),
            step=0.005, format="%.3f", key=f"set_{fkey}_band",
            help="|目标−当前| 权重差小于此值不调仓;策略级可覆盖"))
        ex["stop_loss"] = float(e3.number_input(
            "stop_loss(兜底止损,0=关)", value=num_or(ex.get("stop_loss"), _DEF_EXEC["stop_loss"], float),
            step=0.01, format="%.2f", key=f"set_{fkey}_sl",
            help="执行层兜底:持仓浮亏达此比例强平并阻回补;策略自带止损为主"))
        ex["take_profit"] = float(e4.number_input(
            "take_profit(兜底止盈,0=关)", value=num_or(ex.get("take_profit"), _DEF_EXEC["take_profit"], float),
            step=0.01, format="%.2f", key=f"set_{fkey}_tp"))
        out["execution"] = ex

        section_title("风控（risk，执行层强制）")
        rk = dict(d.get("risk") or {})
        r1, r2, r3 = st.columns(3)
        rk["max_weight_per_symbol"] = float(r1.number_input(
            "单标的权重上限", value=num_or(rk.get("max_weight_per_symbol"), _DEF_RISK["max_weight_per_symbol"], float),
            step=0.01, format="%.2f", key=f"set_{fkey}_mw"))
        rk["max_total_weight"] = float(r2.number_input(
            "总仓位上限", value=num_or(rk.get("max_total_weight"), _DEF_RISK["max_total_weight"], float),
            step=0.05, format="%.2f", key=f"set_{fkey}_mt"))
        rk["max_positions"] = int(r3.number_input(
            "同时持仓数上限(0=不限)", value=num_or(rk.get("max_positions"), _DEF_RISK["max_positions"], int),
            step=1, key=f"set_{fkey}_mp"))
        r4, r5, r6 = st.columns(3)
        rk["max_daily_turnover"] = float(r4.number_input(
            "单日最大调仓(占总资产)", value=num_or(rk.get("max_daily_turnover"), _DEF_RISK["max_daily_turnover"], float),
            step=0.1, format="%.2f", key=f"set_{fkey}_to"))
        rk["liquidity_pct_adv20"] = float(r5.number_input(
            "流动性上限(占20日均额)", value=num_or(rk.get("liquidity_pct_adv20"), _DEF_RISK["liquidity_pct_adv20"], float),
            step=0.01, format="%.2f", key=f"set_{fkey}_lq"))
        rk["targets_max_age_hours"] = float(r6.number_input(
            "targets 时效(小时)", value=num_or(rk.get("targets_max_age_hours"), _DEF_RISK["targets_max_age_hours"], float),
            step=1.0, format="%.0f", key=f"set_{fkey}_age"))
        r7, _, _ = st.columns(3)
        rk["reconcile_tolerance"] = float(r7.number_input(
            "对账容忍度(权重差)",
            value=num_or(rk.get("reconcile_tolerance"), _DEF_RISK["reconcile_tolerance"], float),
            step=0.005, format="%.3f", key=f"set_{fkey}_rt",
            help="次日出信号时比对上一执行日 targets vs fills;超此偏差告警"
                 "(仅提示不阻断)。别设太紧:价格漂移本身就会造成权重偏差"))
        out["risk"] = rk

        section_title("数据源优先级（data.sources）")
        data_cfg = dict(d.get("data") or {})
        current_sources = [str(source) for source in
                           (data_cfg.get("sources") or _DEF["data"]["sources"])]
        known_sources = ["sina", "akshare", "tushare", "qmt"]
        source_options = current_sources + [
            source for source in known_sources if source not in current_sources
        ]
        selected_sources = st.multiselect(
            "按顺序尝试的数据源",
            source_options,
            default=current_sources,
            key=f"set_{fkey}_sources",
            help="前一个源不可用时自动降级到后一个；QMT 仅在交易机或 RPC 可用时启用",
        )
        if selected_sources:
            out["data"] = {**data_cfg, "sources": selected_sources}
        else:
            st.error("至少保留一个数据源")
            out["data"] = {**data_cfg, "sources": []}
        st.caption("新增数据源会追加在末尾；如需精确调整优先顺序，可切换到高级 YAML。")

        text = yaml.safe_dump(out, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
        with st.expander("生成的 YAML 预览"):
            st.code(text, language="yaml")
    else:
        if st.session_state.get("_set_editing") != fkey:
            st.session_state["_set_editing"] = fkey
            st.session_state["set_yaml"] = raw
        text = st.text_area("YAML(含路径等全部配置)", key="set_yaml", height=320)

    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("校验", key="set_check"):
        try:
            Settings(**(yaml.safe_load(text) or {}))
            st.success("配置合法")
        except Exception as ex:                    # noqa: BLE001 展示给用户
            st.error(f"解析失败:{ex}")
    if b2.button("保存", key="set_save"):
        try:
            Settings(**(yaml.safe_load(text) or {}))
            SET_PATH.write_text(text, encoding="utf-8")
            st.cache_data.clear()
            st.success("已保存;涉及路径/缓存的改动建议重启面板生效")
        except Exception as ex:                    # noqa: BLE001
            st.error(f"未保存,配置不合法:{ex}")

    st.divider()
    st.caption("预留:本地大模型、通知渠道等更多本地高级设置将陆续加入此页。")
