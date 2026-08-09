"""
绩效指标与单文件 HTML 报告(方案 §7 · 计划 Task 6)
====================================================================

两个公开接口(run_backtest.py 按此调用):

  compute_stats(result) -> dict
      年化收益(TRADING_DAYS=244)、累计净值、最大回撤、夏普、卡玛、
      年度/月度收益表、换手率、交易配对统计(FIFO 回合)、分标的归因。

  render_html(result, stats, out_path) -> Path
      自包含单文件 HTML:内联 CSS,matplotlib(Agg)图表转 base64 内嵌,
      不引用任何外部资源,可直接邮件/微信发送归档。

指标推导口径(与仓库老脚本 atr_portfolio_backtest.py::stats 对齐):
  年化   cagr = nav_end^(1/years) − 1,years = 交易日数 / 244
  夏普   mean(r)/std(r) × √244(无风险利率取 0,趋势策略惯例)
  回撤   min(nav/cummax(nav) − 1) ≤ 0
  卡玛   cagr / |mdd| —— 每承受 1 单位回撤换来的年化收益
  换手   Σ成交金额 / 平均资产 / years(资产 = init_cash × nav;
         meta 缺资金规模时退化为 Σ|Δw| / years 的权重口径)
  回合   按 symbol 先进先出配对:每笔卖出与最早未平买入撮合成一个回合,
         回合收益 = 卖价/加权买价 − 1(不含费用,只用于胜率/盈亏比诊断)
  归因   contribution[sym] = Σ w[sym].shift(1) × ret[sym]
         (T+1 口径,与引擎记账一致;ret 来自 meta['symbol_returns'],
         引擎未提供时优雅降级,归因置 None、报告跳过该表)
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

from sinan.backtest.result import BacktestResult
from sinan.config import TRADING_DAYS

# CJK 字体:Noto(Linux)→ PingFang(macOS),找不到静默回退(只影响图中中文显示)
for _fp in ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc"]:
    try:
        font_manager.fontManager.addfont(_fp)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=_fp).get_name()
        break
    except Exception:
        pass
plt.rcParams["axes.unicode_minus"] = False

# meta 里可能存放资金规模的键名(引擎/脚本留痕字段,按序探测)
_CAPITAL_KEYS = ("init_cash", "initial_capital", "init_capital", "capital")


# --------------------------------------------------------------------------
# 指标计算
# --------------------------------------------------------------------------
def _pair_trades_fifo(trades: pd.DataFrame) -> np.ndarray:
    """按 symbol FIFO 配对成交流水 → 每个完整回合的收益率数组。

    每笔卖出视为一个回合的终点:从该标的最早未平买入队列中撮合等量份额,
    回合收益 = 卖价 / 加权平均买价 − 1。未配对的买入(仍持仓)不计回合。
    """
    if trades is None or len(trades) == 0:
        return np.array([])
    t = trades.copy()
    t["date"] = pd.to_datetime(t["date"])
    t = t.sort_values("date", kind="stable")

    rounds = []
    queues: dict[str, list[list[float]]] = {}   # symbol -> [[剩余qty, 买价], ...]
    for row in t.itertuples(index=False):
        q = queues.setdefault(row.symbol, [])
        if row.side == "buy":
            q.append([float(row.qty), float(row.price)])
        elif row.side == "sell" and q:
            need, cost_amt, matched = float(row.qty), 0.0, 0.0
            while need > 1e-9 and q:
                lot_qty, lot_px = q[0]
                take = min(need, lot_qty)
                cost_amt += take * lot_px
                matched += take
                need -= take
                q[0][0] -= take
                if q[0][0] <= 1e-9:
                    q.pop(0)
            if matched > 0 and cost_amt > 0:
                avg_buy = cost_amt / matched
                rounds.append(float(row.price) / avg_buy - 1.0)
    return np.array(rounds)


def _annual_turnover(result: BacktestResult, years: float) -> float:
    """换手率 = Σ成交金额 / 平均资产,年化。

    资产规模从 meta 的资金键推(资产 = init_cash × nav);引擎未留痕
    资金规模时退化为权重口径 Σ|Δw|/years(量纲一致,数值近似)。
    """
    trades = result.trades
    if trades is None or len(trades) == 0:
        return 0.0
    notional = float((trades["qty"].astype(float) * trades["price"].astype(float)).sum())
    capital = next((float(result.meta[k]) for k in _CAPITAL_KEYS
                    if isinstance(result.meta, dict) and result.meta.get(k)), None)
    if capital and capital > 0:
        avg_asset = capital * float(result.nav.mean())
        return notional / avg_asset / years
    # 降级:权重变化口径(不依赖资金规模)
    w = result.weights.shift(1).fillna(0.0)
    return float(w.diff().abs().sum().sum()) / years


def compute_stats(result: BacktestResult) -> dict:
    """由 BacktestResult 计算全套绩效指标,返回 dict(供 render_html 消费)。

    键约定:
      annual_return / final_nav / max_drawdown / sharpe / calmar  —— 标量
      annual_turnover / n_trades / win_rate / profit_loss_ratio   —— 标量
      yearly_returns   pd.Series,index=年份
      monthly_returns  pd.DataFrame,index=年份 × columns=月份(1..12)
      symbol_contribution  pd.Series 或 None(meta 缺 symbol_returns 时)
    """
    nav, ret = result.nav, result.returns
    n = len(ret)
    years = max(n, 1) / TRADING_DAYS

    final_nav = float(nav.iloc[-1]) if len(nav) else 1.0
    cagr = final_nav ** (1 / years) - 1
    sd = float(ret.std())
    sharpe = float(ret.mean()) / sd * np.sqrt(TRADING_DAYS) if sd > 0 else 0.0
    mdd = float((nav / nav.cummax() - 1).min()) if len(nav) else 0.0
    calmar = cagr / abs(mdd) if mdd < 0 else np.inf

    # 年度/月度收益:日收益按自然月复利,∏月度 = 累计净值(严格自洽)
    idx = pd.DatetimeIndex(ret.index)
    yearly = (1 + ret).groupby(idx.year).prod() - 1
    yearly.index.name = "年份"
    monthly = ((1 + ret).groupby([idx.year, idx.month]).prod() - 1).unstack()
    monthly.index.name = "年份"
    monthly.columns.name = "月份"

    # 交易回合统计(FIFO 配对)
    rounds = _pair_trades_fifo(result.trades)
    wins, losses = rounds[rounds > 0], rounds[rounds <= 0]
    win_rate = len(wins) / len(rounds) if len(rounds) else np.nan
    plr = (wins.mean() / -losses.mean()
           if len(wins) and len(losses) and losses.mean() < 0 else np.nan)

    # 分标的归因:需要引擎在 meta['symbol_returns'] 提供每标的日收益,缺则跳过
    contrib = None
    sym_ret = result.meta.get("symbol_returns") if isinstance(result.meta, dict) else None
    if isinstance(sym_ret, pd.DataFrame) and not sym_ret.empty:
        cols = [c for c in result.weights.columns if c in sym_ret.columns]
        if cols:
            w_exec = result.weights[cols].shift(1).fillna(0.0)
            r_sym = sym_ret[cols].reindex(result.weights.index).fillna(0.0)
            contrib = (w_exec * r_sym).sum().sort_values(ascending=False)
            contrib.name = "累计贡献"

    return {
        "start": str(idx.min().date()) if n else "",
        "end": str(idx.max().date()) if n else "",
        "n_days": n,
        "years": years,
        "final_nav": final_nav,
        "cum_return": final_nav - 1,
        "annual_return": cagr,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "calmar": calmar,
        "annual_turnover": _annual_turnover(result, years),
        "n_trades": int(len(rounds)),
        "win_rate": win_rate,
        "profit_loss_ratio": plr,
        "yearly_returns": yearly,
        "monthly_returns": monthly,
        "symbol_contribution": contrib,
    }


# --------------------------------------------------------------------------
# HTML 渲染
# --------------------------------------------------------------------------
_ACCENT = "#2e7d64"          # 单一强调色(深海绿)
_POS = "214,69,65"           # A 股习惯:红涨
_NEG = "30,132,73"           # 绿跌

_CSS = f"""
body {{ font-family: "PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
       background: #fff; color: #333; max-width: 1000px; margin: 0 auto;
       padding: 24px 16px; font-size: 14px; }}
h1 {{ font-size: 22px; border-bottom: 3px solid {_ACCENT}; padding-bottom: 8px; }}
h2 {{ font-size: 16px; color: {_ACCENT}; margin-top: 32px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
th, td {{ border: 1px solid #e0e0e0; padding: 5px 10px; text-align: right; }}
th {{ background: {_ACCENT}; color: #fff; font-weight: 500; }}
td:first-child, th:first-child {{ text-align: left; }}
tbody tr:nth-child(even) {{ background: #f4f8f6; }}
img {{ max-width: 100%; }}
.meta {{ color: #888; font-size: 12px; }}
"""


def _fmt_pct(v, digits=1):
    return "—" if v is None or not np.isfinite(v) else f"{v * 100:.{digits}f}%"


def _fmt_num(v, digits=2):
    return "—" if v is None or not np.isfinite(v) else f"{v:.{digits}f}"


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _plot_nav_dd(result: BacktestResult) -> str:
    """净值曲线(对数轴)+ 回撤水下图,返回 base64 PNG。"""
    nav = result.nav
    dd = (nav / nav.cummax() - 1) * 100
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]})
    axes[0].plot(nav.index, nav.values, color=_ACCENT, lw=1.6)
    axes[0].set_yscale("log")
    axes[0].set_title("净值曲线(对数轴)")
    axes[0].grid(alpha=0.3)
    axes[1].fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.45)
    axes[1].set_title("回撤 (%)")
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _monthly_heat_table(monthly: pd.DataFrame, yearly: pd.Series) -> str:
    """月度收益热力表(红涨绿跌,透明度随幅度),末列为全年。"""
    head = "".join(f"<th>{m}月</th>" for m in monthly.columns)
    rows = []
    for year in monthly.index:
        cells = []
        for m in monthly.columns:
            v = monthly.loc[year, m]
            if pd.isna(v):
                cells.append("<td>—</td>")
                continue
            rgb = _POS if v > 0 else _NEG
            alpha = min(abs(v) * 12, 0.85)
            cells.append(f'<td style="background:rgba({rgb},{alpha:.2f})">'
                         f"{v * 100:.1f}%</td>")
        y = yearly.get(year, np.nan)
        cells.append(f"<td><b>{_fmt_pct(y)}</b></td>")
        rows.append(f"<tr><td><b>{year}</b></td>{''.join(cells)}</tr>")
    return (f'<table><thead><tr><th>年份</th>{head}<th>全年</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>")


def _metrics_table(stats: dict) -> str:
    items = [
        ("回测区间", f"{stats['start']} ~ {stats['end']}({stats['n_days']} 个交易日)"),
        ("累计净值", _fmt_num(stats["final_nav"], 4)),
        ("年化收益", _fmt_pct(stats["annual_return"])),
        ("最大回撤", _fmt_pct(stats["max_drawdown"])),
        ("夏普比率", _fmt_num(stats["sharpe"])),
        ("卡玛比率", _fmt_num(stats["calmar"])),
        ("年化换手率", _fmt_num(stats["annual_turnover"])),
        ("交易回合数", str(stats["n_trades"])),
        ("胜率", _fmt_pct(stats["win_rate"])),
        ("盈亏比", _fmt_num(stats["profit_loss_ratio"])),
    ]
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in items)
    return (f'<table><thead><tr><th>指标</th><th>数值</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")


def _contribution_table(contrib: pd.Series | None) -> str:
    if contrib is None or contrib.empty:
        return ""
    rows = "".join(f"<tr><td>{sym}</td><td>{_fmt_pct(v, 2)}</td></tr>"
                   for sym, v in contrib.items())
    return (f"<h2>分标的归因(累计收益贡献 = Σ w·shift(1)×ret)</h2>"
            f'<table><thead><tr><th>标的</th><th>累计贡献</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")


def _trades_table(trades: pd.DataFrame, tail: int = 50) -> str:
    if trades is None or len(trades) == 0:
        return "<p class='meta'>无成交记录。</p>"
    t = trades.copy()
    t["date"] = pd.to_datetime(t["date"]).dt.strftime("%Y-%m-%d")
    t["side"] = t["side"].map({"buy": "买入", "sell": "卖出"}).fillna(t["side"])
    t = t.tail(tail)
    rows = "".join(
        f"<tr><td>{r.date}</td><td>{r.symbol}</td><td>{r.side}</td>"
        f"<td>{r.qty:g}</td><td>{float(r.price):.3f}</td>"
        f"<td>{float(r.cost):.2f}</td><td>{r.reason}</td></tr>"
        for r in t.itertuples(index=False))
    return (f'<table><thead><tr><th>日期</th><th>标的</th><th>方向</th>'
            f"<th>数量</th><th>价格</th><th>费用</th><th>原因</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def render_html(result: BacktestResult, stats: dict, out_path) -> Path:
    """渲染自包含单文件 HTML 报告并写盘,返回输出路径。

    所有图表 PNG 以 base64 内嵌、CSS 内联,不引用任何外部资源;
    meta 留痕(策略名/参数)一并写入页脚,保证报告可独立归档追溯。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    strategy = result.meta.get("strategy", "") if isinstance(result.meta, dict) else ""
    meta_note = {k: v for k, v in (result.meta or {}).items()
                 if not isinstance(v, (pd.DataFrame, pd.Series))}

    img = _plot_nav_dd(result)
    parts = [
        "<meta charset='utf-8'>",
        f"<title>回测报告 {strategy}</title>",
        f"<style>{_CSS}</style>",
        f"<h1>回测绩效报告 {strategy}</h1>",
        "<h2>指标汇总(年化 / 最大回撤 / 夏普 / 卡玛)</h2>",
        _metrics_table(stats),
        "<h2>净值与回撤</h2>",
        f'<img alt="净值曲线与回撤" src="data:image/png;base64,{img}">',
        "<h2>月度收益表</h2>",
        _monthly_heat_table(stats["monthly_returns"], stats["yearly_returns"]),
        _contribution_table(stats.get("symbol_contribution")),
        f"<h2>成交流水(最近 {min(len(result.trades), 50)} 条 / 共 {len(result.trades)} 条)</h2>",
        _trades_table(result.trades),
        f"<p class='meta'>meta: {meta_note}</p>",
    ]
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
