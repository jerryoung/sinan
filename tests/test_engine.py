"""
Task 5 回测引擎测试:T+1 时序 / 涨跌停 / 停牌 / 强制平仓 / 手数 / 现金约束。

全部用合成行情 + 测试内 @register 的可控哑策略,不依赖任何真实策略实现;
费率一律取自 config/rules.yaml(经 resolve_rule),测试内用同一公式独立复算。
本文件同时给 test_costs / test_snapshot 提供共享夹具(make_bars / seed_store 等)。
"""
import math

import numpy as np
import pandas as pd
import pytest

from sinan.backtest.engine import run_backtest
from sinan.config import ExecutionCfg, Settings, StrategyCfg, load_rules
from sinan.data.store import DataStore
from sinan.signal.base import register
from sinan.universe.cb_terms import EVT_LAST_TRADE_DAY
from sinan.universe.instruments import resolve_rule

DAYS = pd.date_range("2024-01-01", periods=8, freq="B")   # 8 个合成交易日
RULES = load_rules()


# --------------------------------------------------------------------------
# 共享夹具
# --------------------------------------------------------------------------
def make_bars(symbol, dates, close, open_=None, adj_factor=1.0,
              up_limit=None, down_limit=None):
    """合成不复权日线(store 的 bars 输入口径:原始价 + adj_factor)。"""
    close = np.asarray(close, dtype=float)
    open_ = close.copy() if open_ is None else np.asarray(open_, dtype=float)
    df = pd.DataFrame({
        "symbol": symbol, "date": dates,
        "open": open_,
        "high": np.maximum(open_, close) * 1.01,
        "low": np.minimum(open_, close) * 0.99,
        "close": close, "volume": 1e6, "amount": close * 1e6,
        "adj_factor": adj_factor,
    })
    if up_limit is not None:
        df["up_limit"] = up_limit
    if down_limit is not None:
        df["down_limit"] = down_limit
    return df


def seed_store(tmp_path, frames, sec_type="etf", calendar=None,
               instruments=None, cb_events=None):
    store = DataStore(tmp_path / "store")
    for df in frames:
        store.write_bars(df, sec_type)
    store.write_calendar(calendar if calendar is not None else DAYS)
    if instruments is not None:
        store.upsert_instruments(instruments)
    if cb_events is not None:
        store.write_cb_events(cb_events)
    return store


def settings_with(band=0.001, price_mode="close"):
    return Settings(execution=ExecutionCfg(price_mode=price_mode, rebalance_band=band))


def cfg_for(symbols, strategy, sec_type="etf", params=None):
    return StrategyCfg(name="t5-test", strategy=strategy, universe=list(symbols),
                       sec_type=sec_type, lookback=50, params=params or {})


def cash_walk(trades, initial):
    """由成交流水重演现金账,顺带断言全程不透支;返回终值。"""
    c = initial
    for r in trades.itertuples():
        if r.side == "buy":
            c -= r.qty * r.price + r.cost
        else:
            c += r.qty * r.price - r.cost
        assert c > -1e-6, f"现金透支: {c}"
    return c


# --------------------------------------------------------------------------
# 可控哑策略(测试专用,勿依赖 donchian)
# --------------------------------------------------------------------------
@register("eng_plan")
def _eng_plan(ctx, plan=None, lookback=0):
    """按日期(YYYY-MM-DD)查表给目标权重;无表项则维持现状(返回当前持仓权重)。"""
    key = ctx.today.strftime("%Y-%m-%d")
    if plan and key in plan:
        return dict(plan[key])
    return dict(ctx.positions)


@register("eng_const")
def _eng_const(ctx, weights=None, lookback=0):
    """每日恒定目标权重 —— 停牌顺延与强平后禁买的试金石。"""
    return dict(weights or {})


def _dstr(i):
    return DAYS[i].strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 1. T+1 时序:t 日信号 → t+1 买入;t+1 归零 → t+2 卖出;bought_today 不阻塞
# --------------------------------------------------------------------------
def test_t_plus_one_order_timing(tmp_path):
    A = "510300"
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8)])
    cfg = cfg_for([A], "eng_plan",
                  params={"plan": {_dstr(0): {A: 0.5}, _dstr(1): {A: 0.0}}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    assert list(tr["side"]) == ["buy", "sell"]
    assert tr["date"].iloc[0] == DAYS[1]          # t 日收盘信号 → t+1 成交
    assert tr["date"].iloc[1] == DAYS[2]          # t+1 归零 → t+2 卖出
    assert tr["qty"].iloc[0] == tr["qty"].iloc[1]  # 隔日卖出,bought_today 不阻塞全额
    assert res.weights.loc[DAYS[1], A] > 0
    assert res.weights.loc[DAYS[2], A] == 0.0
    assert list(tr["reason"].unique()) == ["signal"]


# --------------------------------------------------------------------------
# 2a. 一字涨停:执行日 close = up_limit → 买单不成交,次日顺延成交
# --------------------------------------------------------------------------
def test_limit_up_blocks_buy(tmp_path):
    A = "510300"
    close = [10.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0]
    up = [np.nan, 11.0] + [np.nan] * 6            # d1 显式一字涨停价
    store = seed_store(tmp_path, [make_bars(A, DAYS, close, up_limit=up)])
    cfg = cfg_for([A], "eng_const", params={"weights": {A: 0.5}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    assert (tr["date"] != DAYS[1]).all()          # 涨停日买不进
    assert res.nav.loc[DAYS[1]] == 1.0            # 空仓,净值不动
    assert tr["date"].iloc[0] == DAYS[2]          # 每日重发目标 → 次日成交
    assert tr["side"].iloc[0] == "buy"


# --------------------------------------------------------------------------
# 2b. 一字跌停:卖单不成交,次日顺延卖出
# --------------------------------------------------------------------------
def test_limit_down_blocks_sell(tmp_path):
    A = "510300"
    close = [10.0, 10.0, 10.0, 9.0, 9.0, 10.0, 10.0, 10.0]
    down = [np.nan] * 3 + [9.0] + [np.nan] * 4    # d3 显式一字跌停价
    store = seed_store(tmp_path, [make_bars(A, DAYS, close, down_limit=down)])
    cfg = cfg_for([A], "eng_plan",
                  params={"plan": {_dstr(0): {A: 0.5},
                                   _dstr(2): {A: 0.0}, _dstr(3): {A: 0.0}}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    sells = tr[tr["side"] == "sell"]
    assert (sells["date"] != DAYS[3]).all()       # 跌停日卖不出
    assert res.weights.loc[DAYS[3], A] > 0        # 仓位仍在
    assert sells["date"].iloc[0] == DAYS[4]       # 重发目标 → 次日卖出
    assert sells["qty"].iloc[0] == tr["qty"].iloc[0]   # 全额出清


# --------------------------------------------------------------------------
# 3. 停牌:执行日无 K 线 → 不成交,目标顺延次日成交
# --------------------------------------------------------------------------
def test_suspension_defers_fill(tmp_path):
    A = "510300"
    dates = DAYS.delete(1)                        # d1 停牌(无 K 线)
    store = seed_store(tmp_path, [make_bars(A, dates, [10.0] * len(dates))])
    cfg = cfg_for([A], "eng_const", params={"weights": {A: 0.4}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    assert len(tr) == 1                           # 只成交一次(此后被带宽挡住)
    assert tr["date"].iloc[0] == DAYS[2]          # d1 停牌顺延到 d2
    assert res.nav.loc[DAYS[1]] == 1.0


# --------------------------------------------------------------------------
# 4. 强赎末日强平:当日清零,reason='force_redeem',且无视跌停、此后禁买
# --------------------------------------------------------------------------
def test_force_redeem_on_last_trade_day(tmp_path):
    A = "128100"
    close = [100.0] * 8
    down = [np.nan] * 3 + [100.0] + [np.nan] * 4  # 末交易日"跌停",强平应无视
    events = pd.DataFrame({"symbol": [A], "date": [DAYS[3]],
                           "event": [EVT_LAST_TRADE_DAY]})
    store = seed_store(tmp_path, [make_bars(A, DAYS, close, down_limit=down)],
                       sec_type="cb", cb_events=events)
    cfg = cfg_for([A], "eng_const", sec_type="cb", params={"weights": {A: 0.5}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    buy = tr.iloc[0]
    force = tr[tr["reason"] == "force_redeem"]
    assert len(force) == 1
    assert force["date"].iloc[0] == DAYS[3]       # 末日当天强平(无视跌停)
    assert force["qty"].iloc[0] == pytest.approx(buy["qty"])
    assert res.weights.loc[DAYS[3]:, A].abs().sum() == 0.0   # 清零且不再持有
    assert (tr["date"] <= DAYS[3]).all()          # 强平后禁买,无后续成交
    cash_walk(tr, 1e6)


# --------------------------------------------------------------------------
# 5. 退市强平:instruments.delist_date ≤ t → reason='force_delist'
# --------------------------------------------------------------------------
def test_force_delist(tmp_path):
    A = "510050"
    inst = pd.DataFrame({"symbol": [A], "name": ["样例ETF"], "sec_type": ["etf"],
                         "exchange": ["SH"], "list_date": ["2020-01-01"],
                         "delist_date": [DAYS[4]], "status": ["D"]})
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8)], instruments=inst)
    cfg = cfg_for([A], "eng_const", params={"weights": {A: 0.5}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    force = tr[tr["reason"] == "force_delist"]
    assert len(force) == 1 and force["date"].iloc[0] == DAYS[4]
    assert (tr["date"] <= DAYS[4]).all()          # 退市后禁买
    assert res.weights.loc[DAYS[4]:, A].abs().sum() == 0.0


# --------------------------------------------------------------------------
# 6a. 手数取整:目标金额 12345 元、价格 10 元、lot 100 → 成交 1200 股
# --------------------------------------------------------------------------
def test_lot_rounding(tmp_path):
    A = "510300"
    rule = resolve_rule(A, "etf", RULES)
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8)])
    cfg = cfg_for([A], "eng_plan", params={"plan": {_dstr(0): {A: 0.012345}}})
    res = run_backtest(store, cfg, settings_with(band=0.0005), initial_capital=1e6)

    # d0 空仓 equity 恰为 1e6 → 目标金额 12345 元;执行价含滑点
    px = 10.0 * (1 + rule.slippage)
    expect = math.floor(12345.0 / px / rule.lot_size) * rule.lot_size
    assert expect == 1200
    assert res.trades["qty"].iloc[0] == expect


# --------------------------------------------------------------------------
# 6b. 剩余不足 1 手一次性清仓(经 adj_factor 漂移出零股后触发)
# --------------------------------------------------------------------------
def test_odd_lot_cleanup_sell(tmp_path):
    A = "510300"
    rule = resolve_rule(A, "etf", RULES)
    factors = [1.0, 1.0, 1.0, 1.05, 1.05, 1.05, 1.05, 1.05]   # d3 起分红抬升因子
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8, adj_factor=factors)])
    cfg = cfg_for([A], "eng_plan",
                  params={"plan": {_dstr(0): {A: 0.0125}, _dstr(3): {A: 0.0003}}})
    res = run_backtest(store, cfg, settings_with(band=0.0005), initial_capital=1e6)

    tr = res.trades
    assert list(tr["side"]) == ["buy", "sell"]
    assert tr["qty"].iloc[0] == 1200              # floor(12500/10.002/100)=12 手
    # d4 卖出:目标金额只够 12 手,但剩余 1260−1200=60 股 < 1 手 → 全部清掉
    assert tr["qty"].iloc[1] == pytest.approx(1200 * 1.05)
    assert tr["date"].iloc[1] == DAYS[4]
    assert res.weights.loc[DAYS[4]:, A].abs().sum() == 0.0


# --------------------------------------------------------------------------
# 7. 现金不透支:双标的满仓目标,后成交的买单缩量到可负担整数手
# --------------------------------------------------------------------------
def test_cash_never_negative(tmp_path):
    A, B = "510300", "510500"
    rule = resolve_rule(A, "etf", RULES)
    frames = [make_bars(A, DAYS, [1.0] * 8), make_bars(B, DAYS, [1.0] * 8)]
    store = seed_store(tmp_path, frames)
    cfg = cfg_for([A, B], "eng_const", params={"weights": {A: 0.5, B: 0.5}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    d1 = tr[tr["date"] == DAYS[1]]
    assert list(d1["symbol"]) == [A, B]           # 等权重并列 → 按代码序成交
    # 手工复算:A 足额成交;B 受现金约束缩量
    px = 1.0 * (1 + rule.slippage)
    n_a = math.floor(500_000.0 / px / rule.lot_size) * rule.lot_size
    gross_a = n_a * px
    cash1 = 1e6 - gross_a - gross_a * rule.commission
    n_b_want = math.floor(500_000.0 / px / rule.lot_size) * rule.lot_size
    afford = math.floor(cash1 / (px * (1 + rule.commission)) / rule.lot_size) \
        * rule.lot_size
    n_b = min(n_b_want, afford)
    assert n_b < n_a                              # 缩量确实发生
    assert d1["qty"].iloc[0] == n_a
    assert d1["qty"].iloc[1] == n_b
    end_cash = cash_walk(tr, 1e6)                 # 全程现金不透支
    assert end_cash >= 0


# --------------------------------------------------------------------------
# 8. 目标权重净化:负数截 0,Σ>1 等比缩到满仓
# --------------------------------------------------------------------------
def test_negative_clip_and_rescale(tmp_path):
    A, B = "510300", "510500"
    frames = [make_bars(A, DAYS, [10.0] * 8), make_bars(B, DAYS, [10.0] * 8)]
    store = seed_store(tmp_path, frames)
    cfg = cfg_for([A, B], "eng_const", params={"weights": {A: 2.0, B: -1.0}})
    res = run_backtest(store, cfg, settings_with(), initial_capital=1e6)

    tr = res.trades
    assert (tr["symbol"] == A).all()              # B 负权重截 0,不产生任何成交
    # A 缩到 1.0 满仓,受现金(佣金预留)约束,买入额 ≤ 本金
    first = tr.iloc[0]
    assert first["qty"] * first["price"] + first["cost"] <= 1e6 + 1e-6
    cash_walk(tr, 1e6)


# --------------------------------------------------------------------------
# 持仓数上限:settings.risk.max_positions 在引擎侧生效(与实盘同一实现)
# --------------------------------------------------------------------------
def test_engine_max_positions(tmp_path):
    from sinan.config import RiskCfg
    A, B, C = "510300", "510500", "518880"
    store = seed_store(tmp_path, [make_bars(s, DAYS, [10.0] * 8) for s in (A, B, C)])
    # d0 信号:三只同时给权重,B 最大、C 最小 → 上限 2 只应裁掉 C
    cfg = cfg_for([A, B, C], "eng_plan",
                  params={"plan": {_dstr(0): {A: 0.2, B: 0.3, C: 0.1}}})
    st = Settings(execution=ExecutionCfg(price_mode="close", rebalance_band=0.001),
                  risk=RiskCfg(max_positions=2))
    r = run_backtest(store, cfg, st, initial_capital=1e6)
    n_pos = (r.weights > 1e-9).sum(axis=1)
    assert n_pos.max() == 2                      # 全程从未超过 2 只
    assert (r.weights[C] < 1e-9).all()           # 被裁的是权重最小的 C
    assert set(r.trades["symbol"]) == {A, B}


# --------------------------------------------------------------------------
# 执行层止损/止盈覆盖(settings.execution.stop_loss / take_profit,默认关)
# --------------------------------------------------------------------------
def _settings_overlay(sl=0.0, tp=0.0, band=0.001):
    return Settings(execution=ExecutionCfg(price_mode="close", rebalance_band=band,
                                           stop_loss=sl, take_profit=tp))


def test_overlay_stop_loss_forces_exit_and_blocks_reentry(tmp_path):
    """浮亏破 20% → 次日以 stop_loss 强平;策略仍喊买但引擎禁回补;
    策略目标归零一次后解除,允许再入场。"""
    A = "510300"
    closes = [10.0, 10.0, 8.5, 7.5, 7.5, 7.5, 7.5, 8.2]    # d2起崩,d7温和回升(±10%内可成交)
    plan = {_dstr(0): {A: 0.5},          # d0 信号 → d1 买入@10
            _dstr(5): {},                # d5 策略自行归零 → 解除阻断
            _dstr(6): {A: 0.5}}          # d6 重新喊买 → d7 允许买入
    store = seed_store(tmp_path, [make_bars(A, DAYS, closes)])
    cfg = cfg_for([A], "eng_plan", params={"plan": plan})
    r = run_backtest(store, cfg, _settings_overlay(sl=0.20), initial_capital=1e6)
    tr = r.trades
    # d3 收盘 7.5/10−1=−25% 触发 → d4 卖出,原因 stop_loss
    sells = tr[tr["side"] == "sell"]
    assert list(sells["reason"]) == ["stop_loss"]
    assert sells["date"].iloc[0] == DAYS[4]
    # d4~d6 期间策略计划仍隐含持有(eng_plan 维持现状语义下 d4 目标=现状0,
    # 但 d1-d3 目标 0.5 持续喊买)→ 阻断期无 buy;d7 解除后重新买入
    buys = tr[tr["side"] == "buy"]
    assert list(buys["date"]) == [DAYS[1], DAYS[7]]


def test_overlay_take_profit(tmp_path):
    A = "510300"
    closes = [10.0, 10.0, 11.0, 13.0, 13.0, 13.0, 13.0, 13.0]
    plan = {_dstr(0): {A: 0.5}}
    store = seed_store(tmp_path, [make_bars(A, DAYS, closes)])
    cfg = cfg_for([A], "eng_plan", params={"plan": plan})
    r = run_backtest(store, cfg, _settings_overlay(tp=0.25), initial_capital=1e6)
    sells = r.trades[r.trades["side"] == "sell"]
    # d3 收盘 13/10−1=+30% ≥25% → d4 以 take_profit 平仓
    assert list(sells["reason"]) == ["take_profit"]
    assert sells["date"].iloc[0] == DAYS[4]


def test_overlay_avg_cost_weighted(tmp_path):
    """两笔买入后均价成本加权:止损线基于均价而非首笔价。"""
    A = "510300"
    closes = [10.0, 10.0, 10.8, 10.8, 9.75, 9.0, 9.0, 9.0]
    plan = {_dstr(0): {A: 0.3}, _dstr(1): {A: 0.6}}      # d1@10 买,d2@10.8 加仓
    store = seed_store(tmp_path, [make_bars(A, DAYS, closes)])
    cfg = cfg_for([A], "eng_plan", params={"plan": plan})
    # 均价 ≈ (0.3×10+0.3×10.8)/0.6 ≈ 10.4;9.0/10.4−1 ≈ −13.5%
    r15 = run_backtest(store, cfg, _settings_overlay(sl=0.15), initial_capital=1e6)
    assert "stop_loss" not in set(r15.trades["reason"])   # −13.5% 未破 15%
    r08 = run_backtest(store, cfg, _settings_overlay(sl=0.08), initial_capital=1e6)
    assert "stop_loss" in set(r08.trades["reason"])       # 破 8% 触发(基于加权均价)


def test_overlay_off_keeps_behavior(tmp_path):
    """默认关闭:与未加覆盖层的旧行为逐位一致(快照回归由 test_snapshot 兜底)。"""
    A = "510300"
    closes = [10.0, 10.0, 8.5, 7.5, 7.5, 7.5, 7.5, 10.0]
    plan = {_dstr(0): {A: 0.5}}
    store = seed_store(tmp_path, [make_bars(A, DAYS, closes)])
    cfg = cfg_for([A], "eng_plan", params={"plan": plan})
    r = run_backtest(store, cfg, _settings_overlay(), initial_capital=1e6)
    assert set(r.trades["reason"]) <= {"signal"}          # 无覆盖层原因


def test_rebalance_band_override(tmp_path):
    """StrategyCfg.rebalance_band 覆盖全局带宽:1% 目标默认被 2% 带宽滤掉,
    覆盖为 0.5% 后成交——定投类小增量策略的前提。"""
    A = "510300"
    store = seed_store(tmp_path, [make_bars(A, DAYS, [10.0] * 8)])
    st = Settings(execution=ExecutionCfg(price_mode="close", rebalance_band=0.02))
    cfg0 = cfg_for([A], "eng_const", params={"weights": {A: 0.01}})
    r0 = run_backtest(store, cfg0, st, initial_capital=1e6)
    assert len(r0.trades) == 0                      # 默认带宽:不成交
    cfg1 = StrategyCfg(name="t", strategy="eng_const", universe=[A],
                       sec_type="etf", lookback=50, rebalance_band=0.005,
                       params={"weights": {A: 0.01}})
    r1 = run_backtest(store, cfg1, st, initial_capital=1e6)
    assert len(r1.trades) > 0                       # 覆盖带宽:成交


def test_dca_start_aligned_to_backtest_window(tmp_path):
    """dca 计划起始日以回测窗口起点为准:配置 start 远在窗口之后也应照常
    从窗口首日起投(配置 start 只锚定影子/实盘;此前会导致回测永远无成交)。"""
    A = "510000"
    px = 10.0 + 0.01 * np.arange(len(DAYS))
    store = seed_store(tmp_path, [make_bars(A, DAYS, px)])
    cfg = cfg_for([A], "dca", params={
        "start": "2030-01-01", "freq": "M", "amount": 10000,
        "capital": 100000, "dip_rule": "none"})
    res = run_backtest(store, cfg, settings_with(), initial_capital=100000)
    buys = res.trades[res.trades["side"] == "buy"]
    assert len(buys) >= 1                           # 覆盖生效:首期定投发生
    assert buys["date"].iloc[0] == DAYS[1]          # t0 收盘信号 → t1 执行
    assert res.meta["params"]["start"] == str(DAYS[0].date())   # 留痕为生效值
