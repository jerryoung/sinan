"""设置·实盘配置:命名配置的新增、编辑、设为默认与安全删除。"""
from __future__ import annotations

import hashlib

import streamlit as st

from sinan.config import (LiveProfileCfg, QmtAlgoCfg, QmtExecutionCfg, QmtRpcCfg,
                          load_live_profiles, save_live_profiles)
from sinan.live.qmt_rpc import verify_qmt_rpc
from sinan.live.profiles import (ProfileDeleteBlocked, delete_live_profile,
                                 set_default_live_profile,
                                 upsert_live_profile)
from ui.common import ROOT, enum_ix
from ui.theme import section_title

LIVE_PATH = ROOT / "config" / "live_profiles.yaml"
STRATEGY_DIR = ROOT / "config" / "strategies"
_NEW = "__new__"


def _profile_label(profile_id: str, cfg) -> str:
    profile = cfg.profiles[profile_id]
    suffix = " · 默认" if profile_id == cfg.default else ""
    return f"{profile.name} ({profile_id}){suffix}"


def _show_delete_block(ex: ProfileDeleteBlocked) -> None:
    st.error(str(ex))
    if ex.references:
        st.dataframe([
            {"策略": r.display_name, "策略ID": r.strategy_id, "配置文件": r.path.name}
            for r in ex.references
        ], use_container_width=True, hide_index=True)
    if ex.parse_errors:
        st.warning("以下策略配置无法解析，修复后才能删除："
                   + "、".join(p.name for p in ex.parse_errors))


def render_live_profiles_page() -> None:
    """渲染命名实盘配置集合编辑器。"""
    try:
        cfg = load_live_profiles(LIVE_PATH)
    except Exception as ex:  # noqa: BLE001 配置坏时页面仍要能给出修复入口
        st.error(f"实盘配置无法加载:{ex}")
        return

    raw = LIVE_PATH.read_text(encoding="utf-8")
    fkey = hashlib.md5(raw.encode()).hexdigest()[:8]
    default_profile = cfg.profiles[cfg.default]
    c1, c2 = st.columns(2)
    c1.metric("默认实盘配置", f"{default_profile.name} ({cfg.default})")
    c2.metric("配置数量", len(cfg.profiles))
    st.caption("QMT 下单参数只在这里维护；策略配置仅保存配置 ID 引用。")

    options = list(cfg.profiles) + [_NEW]
    selected = st.selectbox(
        "选择实盘配置",
        options,
        format_func=lambda x: "新增配置" if x == _NEW else _profile_label(x, cfg),
        key=f"live_profile_{fkey}_selected",
    )
    is_new = selected == _NEW
    current = (LiveProfileCfg(name="新 QMT 配置") if is_new
               else cfg.profiles[selected])

    id_col, name_col, engine_col = st.columns([1.2, 2, 1])
    profile_id = id_col.text_input(
        "配置 ID",
        "" if is_new else selected,
        disabled=not is_new,
        placeholder="例如 paper_qmt",
        key=f"live_profile_{fkey}_{selected}_id",
        help="小写字母开头，可含数字、_、-；创建后不可修改",
    ).strip()
    name = name_col.text_input(
        "展示名称",
        current.name,
        key=f"live_profile_{fkey}_{selected}_name",
    )
    engine_col.selectbox(
        "实盘引擎", ["qmt"], disabled=True,
        key=f"live_profile_{fkey}_{selected}_engine",
    )

    section_title("QMT 执行参数")
    account_col, mode_col, offset_col, qty_col = st.columns(4)
    account = account_col.text_input(
        "资金账号（可选）",
        current.qmt.account or "",
        key=f"live_profile_{fkey}_{selected}_account",
        help="当前仅随 targets 留痕；QMT 薄壳仍按模型绑定账号下单，推荐留空",
    )
    modes = ["latest", "limit"]
    quote_mode = mode_col.selectbox(
        "报价方式", modes,
        index=enum_ix(modes, current.qmt.algo.quote_mode, "latest"),
        key=f"live_profile_{fkey}_{selected}_mode",
        help="latest=最新价；limit=按限价偏移报价",
    )
    price_offset = float(offset_col.number_input(
        "限价偏移", min_value=0.0,
        value=float(current.qmt.algo.price_offset), step=0.001, format="%.3f",
        key=f"live_profile_{fkey}_{selected}_offset",
    ))
    max_order_qty = int(qty_col.number_input(
        "单笔拆单上限", min_value=1,
        value=int(current.qmt.algo.max_order_qty), step=1000,
        key=f"live_profile_{fkey}_{selected}_qty",
    ))

    section_title("QMT 数据连接")
    host_col, port_col, timeout_col = st.columns(3)
    host = host_col.text_input(
        "连接地址",
        current.qmt.rpc.host,
        key=f"live_profile_{fkey}_{selected}_rpc_host",
        help="本机或 SSH 隧道用 127.0.0.1；Tailscale 填交易机地址",
    )
    port = int(port_col.number_input(
        "端口",
        min_value=1,
        max_value=65535,
        value=current.qmt.rpc.port,
        step=1,
        key=f"live_profile_{fkey}_{selected}_rpc_port",
    ))
    timeout = float(timeout_col.number_input(
        "超时（秒）",
        min_value=0.1,
        value=current.qmt.rpc.timeout,
        step=1.0,
        key=f"live_profile_{fkey}_{selected}_rpc_timeout",
    ))
    st.caption("数据源中的 QMT 使用默认实盘配置的连接；token 仍只存本机 "
               "~/.qmt_rpc_token，不写入配置文件。")

    verify_col, _ = st.columns([1, 4])
    if verify_col.button(
        "验证 RPC",
        key=f"live_profile_{fkey}_{selected}_rpc_verify",
        disabled=is_new,
        help="验证地址、Token、协议、行情、账号及委托/成交查询，不会产生委托",
    ):
        with st.spinner("正在验证 QMT RPC…"):
            result = verify_qmt_rpc(
                QmtRpcCfg(host=host, port=port, timeout=timeout)
            )
        if result.ready:
            health = result.health
            quote = result.quote
            st.success(f"{result.message} · {result.endpoint}")
            query = result.trade_query
            trade_mode = health.get("trade_mode") or "unknown"
            v1, v2, v3, v4, v5 = st.columns(5)
            v1.metric("QMT 模式", "不可自动检测" if trade_mode == "unknown"
                      else trade_mode)
            v2.metric("RPC 交易转发", "允许" if health.get("allow_trade") else "只读")
            v3.metric("绑定账号", result.account.get("id") or "未识别",
                      help=f"账号状态：{result.account.get('status', '未提供')}")
            v4.metric("委托/成交查询", f"{query.get('orders', 0)} / "
                      f"{query.get('deals', 0)}")
            price = quote.get("last_price")
            v5.metric("实时行情", "—" if price is None else str(price),
                      help=f"{quote.get('symbol')} {quote.get('name')}")
            capabilities = "、".join(health.get("capabilities") or []) or "未提供"
            st.caption(f"服务 {health.get('service')} / 协议 v{health.get('protocol')}；"
                       f"能力：{capabilities}；验证过程未调用任何交易函数。")
            if trade_mode == "unknown":
                st.warning("QMT 模式不可自动检测：RPC 就绪不等于模型已进入实盘运行；"
                           "报单链路请使用独立的仿真交易探针显式验证。")
        else:
            stage_label = {"tcp": "网络", "health": "鉴权/协议",
                           "quote": "行情", "account": "账号",
                           "trade_query": "委托/成交查询"}.get(
                               result.stage, result.stage)
            st.error(f"RPC 未准备就绪 · {result.endpoint} · "
                     f"失败阶段：{stage_label} · {result.message}")

    try:
        edited = LiveProfileCfg(
            name=name,
            engine="qmt",
            qmt=QmtExecutionCfg(
                account=account,
                rpc=QmtRpcCfg(host=host, port=port, timeout=timeout),
                algo=QmtAlgoCfg(
                    quote_mode=quote_mode,
                    price_offset=price_offset,
                    max_order_qty=max_order_qty,
                ),
            ),
        )
    except Exception as ex:  # noqa: BLE001 控件通常已限制,仍防御会话脏状态
        st.error(f"配置不合法:{ex}")
        return

    save_col, default_col, delete_col, _ = st.columns([1, 1, 1, 3])
    if save_col.button("新增" if is_new else "保存修改",
                       key=f"live_profile_{fkey}_{selected}_save"):
        try:
            if is_new and profile_id in cfg.profiles:
                raise ValueError(f"实盘配置 ID 已存在:{profile_id}")
            updated = upsert_live_profile(cfg, profile_id, edited)
            save_live_profiles(updated, LIVE_PATH)
            st.cache_data.clear()
            st.success(f"已保存 {edited.name} ({profile_id})")
            st.rerun()
        except Exception as ex:  # noqa: BLE001 展示校验/文件错误
            st.error(f"保存失败:{ex}")

    if default_col.button(
        "设为默认",
        disabled=is_new or selected == cfg.default,
        key=f"live_profile_{fkey}_{selected}_default",
    ):
        try:
            save_live_profiles(set_default_live_profile(cfg, selected), LIVE_PATH)
            st.cache_data.clear()
            st.success(f"默认实盘配置已切换为 {selected}")
            st.rerun()
        except Exception as ex:  # noqa: BLE001
            st.error(f"切换默认失败:{ex}")

    confirm = False
    if not is_new:
        confirm = st.checkbox(
            "确认删除所选配置",
            key=f"live_profile_{fkey}_{selected}_confirm_delete",
        )
    if delete_col.button(
        "删除",
        disabled=is_new or not confirm,
        type="secondary",
        key=f"live_profile_{fkey}_{selected}_delete",
    ):
        try:
            updated = delete_live_profile(cfg, selected, STRATEGY_DIR)
            save_live_profiles(updated, LIVE_PATH)
            st.cache_data.clear()
            st.success(f"已删除 {selected}")
            st.rerun()
        except ProfileDeleteBlocked as ex:
            _show_delete_block(ex)
        except Exception as ex:  # noqa: BLE001
            st.error(f"删除失败:{ex}")
