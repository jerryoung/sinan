"""设置页:全局 config/settings.yaml 界面化编辑(常用项表单 + 高级 YAML)。

只编辑全局配置;策略级参数在『策略配置』页。控件 key 绑定文件内容指纹
(与策略配置页同一机制),防止外部改动后旧控件状态在保存时回写。
"""
import hashlib

import streamlit as st
import yaml

from sinan.config import Settings
from ui.common import ROOT

SET_PATH = ROOT / "config" / "settings.yaml"


def page():
    st.subheader("设置")
    st.caption("全局配置(config/settings.yaml):本金、执行、风控、通知。"
               "路径类配置(数据仓/targets/报告目录)建议保持默认,如需修改用高级模式。")
    raw = SET_PATH.read_text(encoding="utf-8")
    d = yaml.safe_load(raw) or {}
    fkey = hashlib.md5(raw.encode()).hexdigest()[:8]
    mode = st.radio("配置方式", ["表单", "高级(YAML)"], horizontal=True,
                    key="set_mode")

    if mode == "表单":
        out = dict(d)
        c1, c2 = st.columns(2)
        out["capital"] = float(c1.number_input(
            "capital(全局名义本金,元)", value=float(d.get("capital", 1e6)),
            step=100000.0, format="%.0f", key=f"set_{fkey}_cap",
            help="策略 YAML 未配置专属本金时的缺省值;影子参考委托单与回测初始资金"))
        out["wecom_webhook"] = c2.text_input(
            "企业微信 webhook(空=只写本地日志)", d.get("wecom_webhook", ""),
            type="password", key=f"set_{fkey}_hook",
            help="群机器人 webhook 地址;targets 生成与风控裁剪会推送摘要")

        st.markdown("**执行(execution)**")
        ex = dict(d.get("execution") or {})
        e1, e2, e3, e4 = st.columns(4)
        pm_opts = ["close", "open"]
        ex["price_mode"] = e1.selectbox(
            "price_mode(回测执行价)", pm_opts,
            index=pm_opts.index(ex.get("price_mode", "close")),
            key=f"set_{fkey}_pm", help="close 与实盘 14:45 执行近似对齐")
        ex["rebalance_band"] = float(e2.number_input(
            "rebalance_band(再平衡带宽)", value=float(ex.get("rebalance_band", 0.02)),
            step=0.005, format="%.3f", key=f"set_{fkey}_band",
            help="|目标−当前| 权重差小于此值不调仓;策略级可覆盖"))
        ex["stop_loss"] = float(e3.number_input(
            "stop_loss(兜底止损,0=关)", value=float(ex.get("stop_loss", 0.0)),
            step=0.01, format="%.2f", key=f"set_{fkey}_sl",
            help="执行层兜底:持仓浮亏达此比例强平并阻回补;策略自带止损为主"))
        ex["take_profit"] = float(e4.number_input(
            "take_profit(兜底止盈,0=关)", value=float(ex.get("take_profit", 0.0)),
            step=0.01, format="%.2f", key=f"set_{fkey}_tp"))
        out["execution"] = ex

        st.markdown("**风控(risk,执行层强制)**")
        rk = dict(d.get("risk") or {})
        r1, r2, r3 = st.columns(3)
        rk["max_weight_per_symbol"] = float(r1.number_input(
            "单标的权重上限", value=float(rk.get("max_weight_per_symbol", 0.34)),
            step=0.01, format="%.2f", key=f"set_{fkey}_mw"))
        rk["max_total_weight"] = float(r2.number_input(
            "总仓位上限", value=float(rk.get("max_total_weight", 1.0)),
            step=0.05, format="%.2f", key=f"set_{fkey}_mt"))
        rk["max_positions"] = int(r3.number_input(
            "同时持仓数上限(0=不限)", value=int(rk.get("max_positions", 12)),
            step=1, key=f"set_{fkey}_mp"))
        r4, r5, r6 = st.columns(3)
        rk["max_daily_turnover"] = float(r4.number_input(
            "单日最大调仓(占总资产)", value=float(rk.get("max_daily_turnover", 1.0)),
            step=0.1, format="%.2f", key=f"set_{fkey}_to"))
        rk["liquidity_pct_adv20"] = float(r5.number_input(
            "流动性上限(占20日均额)", value=float(rk.get("liquidity_pct_adv20", 0.05)),
            step=0.01, format="%.2f", key=f"set_{fkey}_lq"))
        rk["targets_max_age_hours"] = float(r6.number_input(
            "targets 时效(小时)", value=float(rk.get("targets_max_age_hours", 8)),
            step=1.0, format="%.0f", key=f"set_{fkey}_age"))
        out["risk"] = rk

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
