"""面板共享层:全局选择、数据访问缓存、targets/报告渲染、表单元数据。

页面模块只 import 本模块与 sinan 包;所有跨页共用的控件(删除弹窗、
报告渲染器、参数表单)集中在这里,保证各页行为一致。
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]

from sinan.config import load_settings  # noqa: E402

NAV_COLOR = "#4269D0"
DD_COLOR = "#C4433F"


# ──────────────────────── 数据访问(带缓存) ────────────────────────
@st.cache_resource
def get_store():
    from sinan.data.store import DataStore
    return DataStore(load_settings().store_root)


@st.cache_data(ttl=3600)
def instruments_df() -> pd.DataFrame:
    df = get_store().read_instruments(sec_type="etf")
    df["symbol"] = df["symbol"].astype(str)
    return df


def etf_names() -> dict:
    df = instruments_df()
    return dict(zip(df["symbol"], df["name"]))


# ──────────────────────── 全局策略选择 ────────────────────────
def strategy_files() -> list[Path]:
    return sorted((ROOT / "config" / "strategies").glob("*.yaml"))


def cfg_label(p: Path) -> str:
    """策略在界面上的展示名:YAML 的 display_name,缺省回退配置文件名。"""
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return str(d.get("display_name") or p.stem)
    except Exception:                              # noqa: BLE001 坏文件仍可选中
        return p.stem


def current_sel() -> tuple[Path, str, str]:
    """(配置路径, name, 展示名)。选择器由 app.py 在量化策略页头部渲染;
    _sel_path 为跨页持久值(控件未渲染时其 key 状态会被 Streamlit 回收)。"""
    p = (st.session_state.get("global_cfg")
         or st.session_state.get("_sel_path") or strategy_files()[0])
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:                              # noqa: BLE001
        d = {}
    return p, str(d.get("name", p.stem)), str(d.get("display_name") or p.stem)


def targets_files(strategy: str | None = None) -> list[Path]:
    d = Path(load_settings().targets_dir)
    if not d.exists():
        return []
    pattern = f"targets_{strategy}_*.json" if strategy else "targets_*.json"
    return sorted(d.glob(pattern), reverse=True)


# ──────────────────────── targets 渲染 ────────────────────────
def show_targets(fp: Path):
    p = json.loads(fp.read_text(encoding="utf-8"))
    names = etf_names()
    tgt = {k: v for k, v in p["targets"].items() if v > 0}
    cap = float(p.get("capital") or load_settings().capital)
    w_sum = sum(tgt.values())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("执行日", p["date"])
    c2.metric("数据截止", str(p["data_cutoff"])[:10])
    c3.metric("本金", f"{cap:,.0f} 元")
    c4.metric("持仓金额", f"{cap * w_sum:,.0f} 元({w_sum:.1%})")
    c5.metric("剩余现金", f"{cap * (1 - w_sum):,.0f} 元({1 - w_sum:.1%})")
    if tgt:
        df = (pd.DataFrame([(s, names.get(s, ""), w) for s, w in tgt.items()],
                           columns=["代码", "名称", "权重"])
              .sort_values("权重", ascending=False).reset_index(drop=True))
        df["权重"] = df["权重"].map(lambda x: f"{x:.1%}")
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("空仓")
    ref = p.get("ref_orders") or []
    if ref:
        st.markdown(f"**参考委托单**(名义本金 {p.get('capital', 0):,.0f} 元;"
                    "参考价为数据截止日收盘,实际数量以执行时点账户与价格为准)")
        rows = [{"代码": o["symbol"], "名称": names.get(o["symbol"], ""),
                 "方向": "买入" if o["side"] == "buy" else "卖出",
                 "目标权重": f"{o['target_w']:.1%}", "权重差": f"{o['delta_w']:+.1%}",
                 "参考价": o["ref_price"], "参考股数": f"{o['qty']:,}",
                 "预估金额": f"{o['est_amount']:,.0f}"} for o in ref]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"策略 {p['strategy']} · 参数指纹 {p['params_fingerprint']} · "
               f"生成于 {p['generated_at']} · 校验和 {p['checksum'][:12]}…")


# ─────────────── 参数元数据(表单化配置的 ❓ 说明) ───────────────
COMMON_HELP = {
    "x_risk": "每笔风险预算:该笔打到止损时允许亏掉组合总资产的比例。"
              "框架:x ≈ 回撤容忍 ÷ 最坏连亏次数 ÷ 预算持仓数(如 0.35/8/10≈0.004)",
    "cap": "单标的权重上限(0.10 = 单只最多占总资产 10%)",
    "atr_n": "ATR 波动率窗口(日),定仓与止损距离的基准",
    "atr_m": "止损/定仓距离 = atr_m × ATR(吊灯止损倍数或定仓分母)",
    "lookback": "信号重放窗口(交易日根数),须覆盖最长持仓周期",
}
STRAT_HELP = {
    "donchian": {"n_entry": "突破入场窗口:收盘 ≥ 前一日的 n 日收盘最高则入场",
                 "n_exit": "反向出场窗口(exit_mode=donchian 时生效)",
                 "exit_mode": "出场方式:atr=吊灯止损(最高收盘−m×ATR);donchian=反向通道"},
    "turtle_s1": {"n_entry": "海龟 S1 突破入场窗口(经典 20 日)",
                  "n_exit": "反向突破出场窗口(经典 10 日)",
                  "n_failsafe": "兜底入场窗口(55 日):信号被过滤器跳过后,创 55 日新高强制上车",
                  "stop_n_mult": "硬止损距离 = 倍数 × N(N=ATR;海龟原始为 2N)",
                  "use_filter": "上次 20 日突破的理论交易若盈利,则跳过本次信号(S1 过滤器)"},
    "livermore": {"k_theta": "市场要诀换栏阈值 θ = 倍数 × ATR(利维摩尔六状态状态机)",
                  "n_tranches": "试探—加码总批数(1/n 试探仓起步)",
                  "add_atr": "价格每上行 倍数×入场ATR 加一批(只加不摊)",
                  "stop_atr": "试探仓认错止损:跌破 入场价 − 倍数×入场ATR 全平",
                  "size_by": "定仓口径:probe=按认错距离(激进);danger=按 2θ 兜底距离(保守)"},
    "xsmom": {"mom_n": "动量回看窗口(244 ≈ 12 个月)",
              "skip_n": "跳过最近窗口(21 ≈ 1 个月,规避短期反转)",
              "top_k": "持有截面动量最强的前 k 名(且动量>0)"},
    "dca": {"start": "定投起始日 YYYY-MM-DD(写死保证可复现)",
            "freq": "定投频率:W 周 / M 月 / Q 季(首个交易日投入)",
            "amount": "每期投入总额(元),篮子内等分",
            "capital": "虚拟账户本金(元),投完自然停止",
            "dip_rule": "下跌加码:none 基准/dip2x 跌破年线2×/dip2x_half 跌2×涨0.5×/tiered 分档",
            "ma_n": "下跌加码的均线窗口(250≈年线)"},
}
ENUM_FIELDS = {"exit_mode": ["atr", "donchian"], "size_by": ["probe", "danger"]}
# 策略私有的枚举取值(覆盖 ENUM_FIELDS 同名项):可选项一律下拉,避免手填出错
STRAT_ENUMS = {
    "dca": {"freq": ["W", "M", "Q"],
            "dip_rule": ["none", "dip2x", "dip2x_half", "tiered"]},
}


def params_grid(strat: str, params: dict, prefix: str) -> dict:
    """一组策略参数的表单格(带 ❓);prefix 保证多策略组合场景下控件 key 唯一。"""
    helps = {**COMMON_HELP, **STRAT_HELP.get(strat, {})}
    enums = {**ENUM_FIELDS, **STRAT_ENUMS.get(strat, {})}
    out = dict(params)
    cols = st.columns(4)
    for i, (k, v) in enumerate(params.items()):
        box = cols[i % 4]
        h = helps.get(k, "(暂无说明,详见策略源码 docstring)")
        key = f"pf_{prefix}_{k}"
        if k in enums:
            opts = enums[k]
            out[k] = box.selectbox(k, opts, index=opts.index(v) if v in opts else 0,
                                   help=h, key=key)
        elif isinstance(v, bool):
            out[k] = box.checkbox(k, value=v, help=h, key=key)
        elif isinstance(v, int):
            out[k] = int(box.number_input(k, value=int(v), step=1, help=h, key=key))
        elif isinstance(v, float):
            out[k] = float(box.number_input(k, value=float(v), step=v / 10 if v else 0.001,
                                            format="%.6f", help=h, key=key))
        else:
            out[k] = box.text_input(k, value=str(v), help=h, key=key)
    return out


def render_param_form(d: dict, prefix: str) -> dict:
    """按参数元数据渲染表单(带 ❓),返回编辑后的配置 dict。
    combo(多策略组合)以嵌套子表单渲染各腿。"""
    strat = d.get("strategy", "")
    out = copy.deepcopy(d)
    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    out["name"] = c1.text_input("name", d.get("name", ""), key=f"pf_{prefix}_name",
                                help="配置名(报告与 targets 留痕用)")
    disp = c2.text_input("display_name(展示名)", d.get("display_name") or "",
                         key=f"pf_{prefix}_disp",
                         help="界面显示名称(纯展示,不参与留痕);留空则显示配置文件名")
    if disp.strip():
        out["display_name"] = disp.strip()
    else:
        out.pop("display_name", None)
    c3.text_input("strategy", strat, disabled=True, key=f"pf_{prefix}_strat",
                  help="策略实现(注册名);换策略请改用高级模式")
    out["lookback"] = int(c4.number_input("lookback", value=int(d.get("lookback", 750)),
                                          step=50, help=COMMON_HELP["lookback"],
                                          key=f"pf_{prefix}_lb"))
    cap_v = float(st.number_input(
        "capital(本金,元;0 = 用全局 settings.capital)",
        value=float(d.get("capital") or 0), step=100000.0, format="%.0f",
        key=f"pf_{prefix}_cfg_capital",
        help="该策略专属本金:影子模式参考委托单与回测初始资金。"
             "优先级:CLI --total-asset > 此处 > 全局 settings.capital"))
    if cap_v > 0:
        out["capital"] = cap_v
    else:
        out.pop("capital", None)
    uni_text = st.text_area("universe(标的池,逗号/空格分隔)",
                            " ".join(str(s) for s in d.get("universe", [])), height=80,
                            key=f"pf_{prefix}_uni",
                            help="回测与出信号的候选池;个数决定等分槽定仓的分母参考")
    out["universe"] = [s.strip().strip('"') for s in uni_text.replace(",", " ").split()
                       if s.strip()]
    st.caption(f"当前池 {len(out['universe'])} 只")

    params = dict(d.get("params", {}))
    if strat == "combo":
        legs_out = []
        for j, leg in enumerate(params.get("legs", [])):
            with st.expander(f"策略 {j + 1}:{leg.get('strategy', '?')}", expanded=True):
                w = st.number_input("weight(该策略资金占比,各策略之和应 ≤ 1)",
                                    value=float(leg.get("weight", 0.5)), step=0.05,
                                    format="%.2f", key=f"pf_{prefix}_leg{j}_w",
                                    help="该策略的目标权重整体乘以此比例后,与其他策略相加")
                p = params_grid(str(leg.get("strategy", "")), dict(leg.get("params", {})),
                                f"{prefix}_leg{j}")
                legs_out.append({"strategy": leg.get("strategy"), "weight": w, "params": p})
        out["params"] = {"legs": legs_out}
    else:
        out["params"] = params_grid(strat, params, prefix)
    return out


def to_yaml(d: dict) -> str:
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False, default_flow_style=None)


# ──────────── 回测报告:结构化落盘 + 统一渲染 ────────────
# 设计原则:一次回测 = 一份报告。实时结果与历史报告用同一份 result.json、
# 同一个 show_report() 渲染,内容天然一致;HTML 文件仅作邮件/归档导出件。
def result_sidecar(rp: Path) -> Path:
    return rp.with_suffix(".result.json")


def save_result_json(res, stats: dict, path: Path) -> None:
    """把 BacktestResult + compute_stats 序列化为统一渲染所需的最小数据集。"""
    scal = {}
    for k, v in stats.items():
        if isinstance(v, (pd.Series, pd.DataFrame)):
            continue
        if isinstance(v, float):
            scal[k] = float(v) if math.isfinite(v) else None
        else:
            scal[k] = v
    monthly = stats["monthly_returns"]
    trades = res.trades
    payload = {
        "stats": scal,
        "yearly": {str(y): float(v) for y, v in stats["yearly_returns"].items()},
        "monthly": {str(y): {str(m): (None if pd.isna(v) else float(v))
                             for m, v in row.items()}
                    for y, row in monthly.iterrows()},
        "nav": {"dates": [d.strftime("%Y-%m-%d") for d in res.nav.index],
                "values": [float(x) for x in res.nav.values]},
        "trades": (trades.assign(date=trades["date"].astype(str).str[:10])
                   .to_dict("records") if len(trades) else []),
        "contribution": ({str(k): float(v)
                          for k, v in stats["symbol_contribution"].items()}
                         if stats.get("symbol_contribution") is not None else None),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _p(v, digits=1):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _n(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}"


def show_report(rp: Path):
    """渲染一份报告(result.json 口径)——指标/净值/回撤/月度热力/成交流水。"""
    data = json.loads(result_sidecar(rp).read_text(encoding="utf-8"))
    s = data["stats"]
    m = st.columns(4)
    m[0].metric("年化收益", _p(s.get("annual_return")))
    m[1].metric("最大回撤", _p(s.get("max_drawdown")))
    m[2].metric("夏普", _n(s.get("sharpe")))
    m[3].metric("卡玛", _n(s.get("calmar")))
    m2 = st.columns(4)
    m2[0].metric("累计净值", _n(s.get("final_nav"), 4))
    m2[1].metric("年化换手", _n(s.get("annual_turnover"), 1))
    m2[2].metric("胜率(FIFO 回合)", _p(s.get("win_rate")))
    m2[3].metric("盈亏比", _n(s.get("profit_loss_ratio")))
    st.caption(f"区间 {s.get('start')} ~ {s.get('end')} · {s.get('n_days')} 个交易日 · "
               f"交易回合 {s.get('n_trades')}")

    nav = pd.Series(data["nav"]["values"],
                    index=pd.to_datetime(data["nav"]["dates"]), name="净值")
    st.markdown("**净值曲线**")
    st.line_chart(nav, color=NAV_COLOR, height=260)
    st.markdown("**回撤**")
    st.area_chart((nav / nav.cummax() - 1).rename("回撤"),
                  color=DD_COLOR, height=140)

    monthly = data.get("monthly") or {}
    if monthly:
        st.markdown("**月度收益热力表**(红涨绿跌,深浅随幅度)")
        years = sorted(monthly)
        mdf = pd.DataFrame(
            [{f"{mm}月": monthly[y].get(str(mm)) for mm in range(1, 13)}
             for y in years], index=years)
        mdf["全年"] = [data.get("yearly", {}).get(y) for y in years]
        mdf = mdf.astype(float)          # None → NaN

        def _cell_map(df, fn):
            return df.map(fn) if hasattr(df, "map") else df.applymap(fn)

        # 显示层预格式化为字符串(NaN → "—"),避免各版本 Styler 对空值
        # 的显示差异;背景色按数值矩阵另行着色
        disp = _cell_map(mdf, lambda v: "—" if pd.isna(v) else f"{v*100:.1f}%")

        def _heat(v):
            if pd.isna(v):
                return ""
            rgb = "214,69,65" if v > 0 else "30,132,73"
            return f"background-color: rgba({rgb},{min(abs(v) * 12, 0.85):.2f})"

        st.dataframe(disp.style.apply(lambda _: _cell_map(mdf, _heat), axis=None),
                     width="stretch")

    contrib = data.get("contribution")
    if contrib:
        st.markdown("**分标的归因(累计贡献)**")
        names = etf_names()
        cdf = pd.DataFrame(
            [(k, names.get(k, ""), f"{v*100:.2f}%")
             for k, v in sorted(contrib.items(), key=lambda kv: -kv[1])],
            columns=["代码", "名称", "累计贡献"])
        st.dataframe(cdf, width="stretch", hide_index=True)

    st.markdown("**成交流水**")
    trs = data.get("trades") or []
    if not trs:
        st.info("无成交记录")
        return
    tr = pd.DataFrame(trs)
    names = etf_names()
    tr["名称"] = tr["symbol"].astype(str).map(lambda x: names.get(x, ""))
    tr["方向"] = tr["side"].map({"buy": "买入", "sell": "卖出"}).fillna(tr["side"])
    tr["原因"] = tr["reason"].map({"signal": "信号", "stop_loss": "止损",
                                   "take_profit": "止盈", "force_redeem": "强赎强平",
                                   "force_delist": "退市强平"}).fillna(tr["reason"])
    tr = tr[["date", "symbol", "名称", "方向", "qty", "price", "cost", "原因"]]
    tr.columns = ["日期", "代码", "名称", "方向", "数量", "价格", "费用", "原因"]
    tr = tr.sort_values("日期", ascending=False).reset_index(drop=True)
    f1, f2, f3 = st.columns([2, 1, 1])
    kw = f1.text_input("筛选(代码/名称)", "", key=f"tr_kw_{rp.stem}")
    if kw:
        tr = tr[tr["代码"].astype(str).str.contains(kw)
                | tr["名称"].str.contains(kw, na=False)]
    page_size = int(f2.selectbox("每页", [20, 50, 100], index=0,
                                 key=f"tr_ps_{rp.stem}"))
    n_pages = max(1, -(-len(tr) // page_size))
    page = int(f3.number_input(f"页码(共 {n_pages} 页 / {len(tr)} 笔)",
                               min_value=1, max_value=n_pages, value=1,
                               key=f"tr_pg_{rp.stem}"))
    view = tr.iloc[(page - 1) * page_size: page * page_size]
    st.dataframe(view, width="stretch", hide_index=True)


@st.dialog("确认删除")
def confirm_delete(target: Path, extras: list[Path] | None = None,
                   label: str = "文件", reset_key: str | None = None):
    """通用删除确认弹窗;extras 为随主文件一并删除的附属文件(配置快照、
    结构化结果等);reset_key 指定删除后需重置的控件状态(如报告选择框)。"""
    st.write(f"确定删除{label} **{target.name}** 吗?此操作不可恢复。")
    b1, b2 = st.columns(2)
    if b1.button("确认删除", type="primary", key="dlg_del_yes"):
        target.unlink(missing_ok=True)
        for e in extras or []:
            e.unlink(missing_ok=True)
        if reset_key:
            st.session_state.pop(reset_key, None)
        st.rerun()
    if b2.button("取消", key="dlg_del_no"):
        st.rerun()
