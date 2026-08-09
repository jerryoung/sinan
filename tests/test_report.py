"""Task 6 绩效报告测试:compute_stats 关键指标 + render_html 单文件 HTML smoke。

构造 200 日合成行情的假 BacktestResult(3 标的,固定 seed 可复现):
  - meta['symbol_returns'] 存在时:归因表可算、关键指标齐全且数值合理;
  - meta 缺 symbol_returns 时:优雅降级,不抛异常;
  - render_html 产出的 HTML 含"年化/最大回撤/月度"等关键区块且图片已 base64 内嵌。
"""
import numpy as np
import pandas as pd
import pytest

from trend.backtest.report import compute_stats, render_html
from trend.backtest.result import TRADE_COLS, BacktestResult

N_DAYS = 200
SYMS = ["510300", "518880", "513100"]


def _make_result(with_symbol_returns=True) -> BacktestResult:
    """200 日随机行情 → 权重×收益合成净值,外加一段确定性成交流水。"""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-02", periods=N_DAYS, freq="B")

    # 各标的日收益:轻微正漂移 + 噪声,保证净值曲线有涨有跌(回撤非零)
    sym_ret = pd.DataFrame(
        rng.normal(0.0005, 0.015, size=(N_DAYS, len(SYMS))),
        index=dates, columns=SYMS)

    # 权重:分段持仓(前 1/3 只持第一只,中段满配,尾段空仓),模拟趋势进出场
    w = pd.DataFrame(0.0, index=dates, columns=SYMS)
    w.iloc[: N_DAYS // 3, 0] = 0.30
    w.iloc[N_DAYS // 3: 2 * N_DAYS // 3, :] = 0.25
    # 组合收益 = Σ 前一日权重 × 当日标的收益(T+1 口径,与引擎一致)
    port_ret = (w.shift(1).fillna(0.0) * sym_ret).sum(axis=1)
    nav = (1 + port_ret).cumprod()

    # 成交流水:两个完整回合,盈亏各一,便于精确断言配对统计
    trades = pd.DataFrame([
        # 回合1:510300 买100@10 → 卖100@11,盈利 +10%
        (dates[5], "510300", "buy", 100, 10.0, 8.0, "signal"),
        (dates[60], "510300", "sell", 100, 11.0, 9.0, "signal"),
        # 回合2:518880 买200@5 → 卖200@4.5,亏损 −10%
        (dates[70], "518880", "buy", 200, 5.0, 8.0, "signal"),
        (dates[130], "518880", "sell", 200, 4.5, 7.0, "signal"),
        # 未平仓的买入:不构成完整回合,不应计入笔数
        (dates[140], "513100", "buy", 300, 2.0, 5.0, "signal"),
    ], columns=TRADE_COLS)

    meta = {"strategy": "test", "init_cash": 1_000_000.0}
    if with_symbol_returns:
        meta["symbol_returns"] = sym_ret
    return BacktestResult(nav=nav, returns=port_ret, weights=w,
                          trades=trades, meta=meta)


@pytest.fixture
def result():
    return _make_result(with_symbol_returns=True)


# ---------------------------------------------------------------- compute_stats
def test_stats_keys_and_sanity(result):
    st = compute_stats(result)
    for key in ["annual_return", "final_nav", "max_drawdown", "sharpe", "calmar",
                "annual_turnover", "n_trades", "win_rate", "profit_loss_ratio",
                "yearly_returns", "monthly_returns", "symbol_contribution"]:
        assert key in st, f"缺少关键指标 {key}"

    assert st["max_drawdown"] <= 0                      # 回撤按负数口径
    assert np.isfinite(st["annual_return"])
    assert st["final_nav"] > 0
    assert np.isfinite(st["sharpe"])
    # 累计收益与年化自洽:final_nav = (1+annual)^years
    years = len(result.returns) / 244
    assert st["final_nav"] == pytest.approx((1 + st["annual_return"]) ** years)


def test_stats_trade_pairing(result):
    """FIFO 配对:2 个完整回合(+10% / −10%),胜率 50%,盈亏比 1。"""
    st = compute_stats(result)
    assert st["n_trades"] == 2
    assert st["win_rate"] == pytest.approx(0.5)
    assert st["profit_loss_ratio"] == pytest.approx(1.0, rel=1e-6)


def test_stats_turnover(result):
    """换手率 = 成交金额 / 平均资产,年化;>0 且有限。"""
    st = compute_stats(result)
    assert np.isfinite(st["annual_turnover"]) and st["annual_turnover"] > 0
    # 上界粗校验:总成交金额 4600 元 / 平均资产 ~1e6 / 0.82 年,应远小于 1
    assert st["annual_turnover"] < 0.1


def test_stats_monthly_yearly(result):
    st = compute_stats(result)
    mt = st["monthly_returns"]
    assert isinstance(mt, pd.DataFrame)
    assert set(mt.columns).issubset(range(1, 13))       # 列 = 月份
    assert list(mt.index) == [2024]                     # 行 = 年份
    # 月度复利 ≈ 累计:∏(1+月收益) ≈ final_nav
    total = (1 + mt.stack()).prod()
    assert total == pytest.approx(st["final_nav"], rel=1e-9)
    yr = st["yearly_returns"]
    assert yr.loc[2024] == pytest.approx(st["final_nav"] - 1, rel=1e-9)


def test_stats_symbol_contribution(result):
    """归因:contribution[sym] = Σ w.shift(1)×ret,总和 ≈ 组合累计算术收益。"""
    st = compute_stats(result)
    contrib = st["symbol_contribution"]
    assert isinstance(contrib, pd.Series) and set(contrib.index) == set(SYMS)
    assert contrib.sum() == pytest.approx(result.returns.sum(), rel=1e-9)


def test_stats_missing_symbol_returns_degrades():
    """meta 无 symbol_returns:不抛异常,归因置 None,其余指标照常。"""
    res = _make_result(with_symbol_returns=False)
    st = compute_stats(res)
    assert st["symbol_contribution"] is None
    assert st["max_drawdown"] <= 0


def test_stats_empty_trades():
    """空成交流水:笔数 0,胜率/盈亏比 NaN,换手 0,不抛异常。"""
    res = _make_result()
    res.trades = pd.DataFrame(columns=TRADE_COLS)
    st = compute_stats(res)
    assert st["n_trades"] == 0
    assert np.isnan(st["win_rate"]) and np.isnan(st["profit_loss_ratio"])
    assert st["annual_turnover"] == 0.0


# ---------------------------------------------------------------- render_html
def test_render_html_smoke(result, tmp_path):
    st = compute_stats(result)
    out = render_html(result, st, tmp_path / "report.html")
    assert out.exists() and out.suffix == ".html"
    html = out.read_text(encoding="utf-8")
    for block in ["年化", "最大回撤", "月度", "归因", "成交流水"]:
        assert block in html, f"HTML 缺少区块:{block}"
    assert "data:image/png;base64," in html             # 图表已内嵌
    assert "<script src=" not in html and "http" not in html.split("</style>")[0]


def test_render_html_without_symbol_returns(tmp_path):
    """缺归因数据时 HTML 仍能产出(跳过归因表)。"""
    res = _make_result(with_symbol_returns=False)
    st = compute_stats(res)
    out = render_html(res, st, tmp_path / "r2.html")
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "年化" in html and "最大回撤" in html
