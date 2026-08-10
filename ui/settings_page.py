"""设置页:全局 config/settings.yaml 界面化编辑(常用项表单 + 高级 YAML)。

只编辑全局配置;策略级参数在『策略配置』页。控件 key 绑定文件内容指纹
(与策略配置页同一机制),防止外部改动后旧控件状态在保存时回写。
"""
import hashlib

import streamlit as st
import yaml

from sinan.config import ExecutionCfg, RiskCfg, Settings
from ui.common import ROOT, enum_ix, num_or

SET_PATH = ROOT / "config" / "settings.yaml"

# 表单缺省一律取自 pydantic 模型,不在界面上另写一份字面量:两份默认值
# 迟早漂移(max_positions 曾表单 12 / 模型 0,配置缺该键时面板显示 12,
# 一次保存就把"不限"静默改成 12)。
_DEF = Settings().model_dump()
_DEF_EXEC = ExecutionCfg().model_dump()
_DEF_RISK = RiskCfg().model_dump()


def page():
    st.subheader("设置")
    st.caption("全局配置(config/settings.yaml):本金、执行、风控、实盘、通知。"
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
            "capital(全局名义本金,元)", value=float(d.get("capital", _DEF["capital"])),
            step=100000.0, format="%.0f", key=f"set_{fkey}_cap",
            help="策略 YAML 未配置专属本金时的缺省值;影子参考委托单与回测初始资金"))
        out["wecom_webhook"] = c2.text_input(
            "企业微信 webhook(空=只写本地日志)", d.get("wecom_webhook", _DEF["wecom_webhook"]),
            type="password", key=f"set_{fkey}_hook",
            help="群机器人 webhook 地址;targets 生成与风控裁剪会推送摘要")

        st.markdown("**执行(execution)**")
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

        st.markdown("**风控(risk,执行层强制)**")
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
        out["risk"] = rk

        st.markdown("**实盘设置(live)**")
        lv = dict(d.get("live") or {})
        lq = dict(lv.get("qmt") or {})
        la = dict(lq.get("algo") or {})
        lv["engine"] = st.selectbox(
            "engine(默认实盘引擎)", eng_opts := ["qmt"],
            index=enum_ix(eng_opts, lv.get("engine"), "qmt"), key=f"set_{fkey}_le",
            help="targets 的执行通道;当前仅支持 QMT 薄壳(文件桥接),预留扩展")
        # 启用开关:不启用则保持 qmt 为空,targets 不带 qmt 字段、薄壳用自己的
        # 缺省算法(随薄壳 LOT 联动)。无此开关时,任何一次表单保存都会把空配置
        # 静默固化成当前界面值——把"跟随薄壳缺省"悄悄换成"锁死本地快照"
        use_qmt = st.checkbox(
            "配置全局 QMT 执行参数(不勾选 = 交由 QMT 薄壳内置缺省)",
            value=bool(lq), key=f"set_{fkey}_lqon")
        if use_qmt:
            acct = st.text_input(
                "qmt.account(资金账号,空=用 QMT 侧绑定账号)",
                lq.get("account", ""), key=f"set_{fkey}_lacct",
                help="当前薄壳按 QMT 绑定账号下单,此字段仅随 targets 留痕"
                     "(多账号扩展预留);推荐留空")
            a1, a2, a3 = st.columns(3)
            la["quote_mode"] = a1.selectbox(
                "qmt.algo.quote_mode(报价方式)", qm_opts := ["latest", "limit"],
                index=enum_ix(qm_opts, la.get("quote_mode"), "latest"),
                key=f"set_{fkey}_lqm",
                help="latest=最新价市价单;limit=限价单(带偏移)")
            la["price_offset"] = float(a2.number_input(
                "qmt.algo.price_offset(限价偏移)",
                value=num_or(la.get("price_offset"), 0.002, float), step=0.001,
                format="%.3f", key=f"set_{fkey}_lpo",
                help="限价单相对最新价的让价比例:买挂高、卖挂低"))
            la["max_order_qty"] = int(a3.number_input(
                "qmt.algo.max_order_qty(单笔拆单上限,股)",
                value=num_or(la.get("max_order_qty"), 10000, int), step=1000,
                key=f"set_{fkey}_lmq", help="超过则拆成多笔委托"))
            if acct.strip():
                lq["account"] = acct.strip()
            else:
                lq.pop("account", None)
            lq["algo"] = la
            lv["qmt"] = lq
        else:
            lv["qmt"] = {}
        out["live"] = lv
        st.caption("全局缺省:策略 YAML 未配置 qmt 时,targets 携带此处配置;"
                   "策略级 qmt(策略配置页)**整体覆盖**此处,不做键级合并。")

        st.markdown("**实盘设置 · QMT 远端接口(qmt_rpc)**")
        qr = dict(d.get("qmt_rpc") or {})
        q1, q2, q3 = st.columns(3)
        qr["host"] = q1.text_input(
            "host", qr.get("host", "127.0.0.1"), key=f"set_{fkey}_qh",
            help="SSH 隧道场景保持 127.0.0.1;Tailscale 场景填 ECS 的 100.x IP")
        qr["port"] = int(q2.number_input(
            "port", value=int(qr.get("port", 58620)), step=1, key=f"set_{fkey}_qp"))
        qr["timeout"] = float(q3.number_input(
            "timeout(秒)", value=float(qr.get("timeout", 15.0)), step=5.0,
            key=f"set_{fkey}_qt"))
        out["qmt_rpc"] = qr
        st.caption("⚠️ token 不在此配置:写入本机 `~/.qmt_rpc_token`(单行,建议 "
                   "`chmod 600`),严禁进仓库/配置/日志。远程访问务必走 SSH 隧道"
                   "或 Tailscale,token 只是第二道锁——见 docs/qmt-deploy.md。")

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
