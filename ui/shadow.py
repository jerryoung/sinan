"""策略看板：影子/实盘统一入口与研究工作台。"""
from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sinan.config import (load_live_profiles, load_settings, load_strategy,
                          resolve_live_profile)
from sinan.live import ledger
from ui.common import (NAV_COLOR, ROOT, confirm_delete, current_sel,
                       etf_names, get_store, show_targets, targets_files)
from ui.theme import (metric_strip, page_header, section_title, status_kv,
                      workflow_bar)

_MODE = {
    "real": "实盘运行",
    "sim": "模拟盘运行",
    "backtest": "QMT 回测",
    "unknown": "实盘运行（模式未知）",
}


def _fmt(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


@st.cache_data(ttl=300)
def _shadow_nav_cached(strategy: str, n_targets: int) -> pd.Series:
    """影子净值；n_targets 参与缓存 key，新 targets 生成后自动失效。"""
    return ledger.shadow_nav(get_store(), load_settings().targets_dir, strategy)


def _latest_target(strategy: str) -> tuple[list, dict | None]:
    files = targets_files(strategy)
    if not files:
        return files, None
    try:
        return files, json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return files, None


def _run_update(strategy_path) -> None:
    with st.spinner("拉数 → 质检 → 出信号（约 1～2 分钟）…"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "shadow_update.py"),
             "--strategy", str(strategy_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    st.code((result.stdout + result.stderr)[-3000:] or "（无输出）")
    if result.returncode == 0:
        st.cache_data.clear()
        st.success("数据更新与 targets 生成完成")
    else:
        st.error("任务失败：质检未通过或数据源不可用，未生成新 targets。")


def _render_research(nav: pd.Series, stats: dict) -> None:
    with st.container(border=True):
        section_title("组合净值与回撤研究")
        metric_strip([
            ("累计收益", _fmt(stats["cum"]), "danger" if stats["cum"] < 0 else "success"),
            ("年化收益", _fmt(stats["annual"]), "danger" if stats["annual"] < 0 else "success"),
            (f"日涨幅 · {stats['daily_date']}", _fmt(stats["daily"]),
             "danger" if stats["daily"] < 0 else "success"),
            ("最大回撤", _fmt(stats["mdd"]), "danger"),
            ("跟踪交易日", f"{stats['n_days']} 天", ""),
        ])
        series = (nav - 1) * 100
        figure = go.Figure(go.Scatter(
            x=series.index,
            y=series.values,
            mode="lines",
            line={"color": NAV_COLOR, "width": 2},
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>",
        ))
        figure.update_layout(
            height=270,
            margin={"l": 8, "r": 8, "t": 6, "b": 8},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"color": "#93A0B1", "size": 11},
            hovermode="x unified",
            showlegend=False,
        )
        figure.update_xaxes(showgrid=False, linecolor="#273241")
        figure.update_yaxes(gridcolor="#273241", zerolinecolor="#354255",
                            ticksuffix="%")
        st.plotly_chart(
            figure,
            width="stretch",
            config={"displayModeBar": False, "responsive": True},
        )
        with st.expander("策略表现（完整指标）"):
            rows = [
                ("累计收益", _fmt(stats["cum"])),
                ("年化收益", _fmt(stats["annual"])),
                (f"日涨幅（{stats['daily_date']}）", _fmt(stats["daily"])),
                ("月涨幅（本月）", _fmt(stats["mtd"])),
                ("近一月", _fmt(stats["m1"])),
                ("近三月", _fmt(stats["m3"])),
                ("近六月", _fmt(stats["m6"])),
                ("近一年", _fmt(stats["y1"])),
                ("年初至今", _fmt(stats["ytd"])),
                ("最大回撤", _fmt(stats["mdd"])),
                ("最大回撤区间", f"{stats['mdd_start']} → {stats['mdd_end']}"),
                ("跟踪交易日", str(stats["n_days"])),
            ]
            st.dataframe(pd.DataFrame(rows, columns=["指标", "数值"]),
                         width="stretch", hide_index=True)
            st.caption("不足回看窗口的指标显示“—”；影子口径不含费用，接入 QMT "
                       "后自动切换为账户真值。")


def _render_holdings(*, live: bool, fills: list[dict], target: dict | None,
                     settings, names: dict) -> int:
    section_title("持仓详情")
    if live:
        last = fills[-1]
        positions = last.get("positions") or {}
        total = float(last.get("total_asset") or 0)
        summary = st.columns(3)
        summary[0].metric("总资产", f"{total:,.0f} 元")
        summary[1].metric("现金", f"{float(last.get('cash') or 0):,.0f} 元")
        summary[2].metric("回报日期", str(last.get("date", "—")))
        if positions:
            rows = [{
                "代码": symbol,
                "名称": names.get(symbol, ""),
                "数量": f"{position.get('qty', 0):,.0f}",
                "最新价": position.get("price", 0.0),
                "市值": f"{position.get('qty', 0) * position.get('price', 0.0):,.0f}",
                "权重": f"{(last.get('weights') or {}).get(symbol, 0):.1%}",
            } for symbol, position in positions.items()]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("当前空仓")
        return len(positions)

    if target:
        capital = float(target.get("capital") or settings.capital)
        weights = {key: value for key, value in target.get("targets", {}).items()
                   if value > 0}
        if weights:
            rows = [{
                "代码": symbol,
                "名称": names.get(symbol, ""),
                "权重": f"{weight:.1%}",
                "目标金额": f"{capital * weight:,.0f}",
            } for symbol, weight in sorted(weights.items(), key=lambda item: -item[1])]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.caption(f"影子口径：目标仓位 × 本金 {capital:,.0f} 元，"
                       f"执行日 {target.get('date', '—')}；接入 QMT 后显示真实持仓。")
        else:
            st.info("当前目标为空仓")
        return len(weights)

    st.info("尚无 targets，请从右侧运行数据更新与信号生成。")
    return 0


def _render_decision_rail(*, live: bool, fills: list[dict], stats: dict,
                          target: dict | None, profile_id: str, profile,
                          settings, position_count: int, strategy_path) -> None:
    with st.container(border=True):
        section_title("运行状态")
        mode = (_MODE.get(str(fills[-1].get("trade_mode", "unknown")),
                          "实盘运行") if live else "影子运行中")
        status_kv("当前模式", mode, tone="success" if live else "primary")
        status_kv("数据来源", "QMT 回报" if live else "targets 影子重放")
        if live:
            status_kv("资金账号", fills[-1].get("account", "—"))

    with st.container(border=True):
        section_title("数据概况")
        status_kv("数据截止", str((target or {}).get("data_cutoff", "—"))[:10])
        status_kv("执行日", (target or {}).get("date", "—"))
        generated = str((target or {}).get("generated_at", "—")).replace("T", " ")
        status_kv("targets 生成", generated[:16])

    with st.container(border=True):
        section_title("实盘配置")
        status_kv("配置名称", profile.name)
        status_kv("配置 ID", profile_id, tone="primary")
        status_kv("实盘引擎", profile.engine)
        status_kv("报价方式", profile.qmt.algo.quote_mode)

    with st.container(border=True):
        section_title("风险概览")
        status_kv("最大回撤", _fmt(stats.get("mdd")),
                  tone="danger" if stats.get("mdd", 0) < 0 else "")
        status_kv("单标的上限", f"{settings.risk.max_weight_per_symbol:.0%}")
        status_kv("总仓位上限", f"{settings.risk.max_total_weight:.0%}")
        max_positions = settings.risk.max_positions
        position_text = (f"{position_count} / {max_positions}"
                         if max_positions else f"{position_count} / 不限")
        status_kv("持仓数量", position_text)

    if st.button("更新数据并生成 targets", type="primary",
                 width="stretch", key="shadow_update_targets"):
        _run_update(strategy_path)


def _render_history(*, live: bool, fills: list[dict], target_files: list,
                    strategy_name: str, settings, names: dict) -> None:
    history_tabs = st.tabs(["交易记录", "targets 详情"])
    with history_tabs[0]:
        if live:
            trades = ledger.live_trades(fills)
            if len(trades):
                trades["名称"] = trades["symbol"].astype(str).map(
                    lambda symbol: names.get(symbol, ""))
                trades["side"] = trades["side"].map({"buy": "买入", "sell": "卖出"})
                trades = trades[["date", "symbol", "名称", "side", "qty", "price"]]
                trades.columns = ["日期", "代码", "名称", "方向", "数量", "委托价"]
                st.dataframe(trades.sort_values("日期", ascending=False).head(200),
                             width="stretch", hide_index=True)
                st.caption(f"共 {len(trades)} 笔，QMT 回报口径；显示最近 200 笔。")
            else:
                st.info("暂无委托记录")
        else:
            history = ledger.load_targets_history(settings.targets_dir, strategy_name)
            rows = []
            for payload in history:
                for order in payload.get("ref_orders") or []:
                    rows.append({
                        "执行日": payload["date"],
                        "代码": order["symbol"],
                        "名称": names.get(order["symbol"], ""),
                        "方向": "买入" if order["side"] == "buy" else "卖出",
                        "参考股数": order["qty"],
                        "参考价": order["ref_price"],
                        "预估金额": f"{order['est_amount']:,.0f}",
                    })
            if rows:
                st.dataframe(pd.DataFrame(rows).sort_values("执行日", ascending=False),
                             width="stretch", hide_index=True)
                st.caption("影子口径：历次 targets 的参考委托单，不代表真实成交。")
            else:
                st.info("暂无参考委托记录")

    with history_tabs[1]:
        if not target_files:
            st.info("尚无 targets 文件")
            return
        show_targets(target_files[0])
        st.divider()
        left, right = st.columns([4, 1])
        selected = left.selectbox("历史 targets", target_files,
                                  format_func=lambda path: path.name)
        right.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
        if right.button("删除", key="tgt_del"):
            confirm_delete(selected, label="targets 文件")
        if selected != target_files[0]:
            show_targets(selected)


def page() -> None:
    strategy_path, strategy_name, strategy_label = current_sel()
    settings = load_settings()
    strategy_cfg = load_strategy(strategy_path)
    profile_id, profile = resolve_live_profile(load_live_profiles(), strategy_cfg)
    fills = ledger.load_fills(settings.fills_dir, strategy_name)
    live = bool(fills)
    target_files, latest_target = _latest_target(strategy_name)

    page_header("策略看板")
    workflow_bar("live")

    nav = (ledger.live_nav(fills) if live
           else _shadow_nav_cached(strategy_name, len(target_files)))
    stats = ledger.perf_stats(nav)
    names = etf_names()

    analysis_col, rail_col = st.columns([3.35, 1], gap="medium")
    with analysis_col:
        if stats:
            _render_research(nav, stats)
        else:
            st.info("跟踪数据不足，需要至少两个交易日净值。生成 targets 并跟踪"
                    "几天后，这里会显示收益与回撤。")
        position_count = _render_holdings(
            live=live,
            fills=fills,
            target=latest_target,
            settings=settings,
            names=names,
        )

    with rail_col:
        _render_decision_rail(
            live=live,
            fills=fills,
            stats=stats or {},
            target=latest_target,
            profile_id=profile_id,
            profile=profile,
            settings=settings,
            position_count=position_count,
            strategy_path=strategy_path,
        )

    section_title("运行记录")
    _render_history(
        live=live,
        fills=fills,
        target_files=target_files,
        strategy_name=strategy_name,
        settings=settings,
        names=names,
    )
