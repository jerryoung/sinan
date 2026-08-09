"""
逐日事件循环回测引擎(方案 §7.2)—— 整个系统正确性的核心,精度优先。

对外唯一入口:

    run_backtest(store, strategy_cfg, settings, start=None, end=None,
                 initial_capital=1_000_000.0) -> BacktestResult

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
逐日循环:每个交易日 t 严格按以下顺序执行
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 执行 t−1 收盘信号排队的委托,按执行价模型(execution_model)撮合:
   执行价 = price_mode('close'/'open')基准价 ± 滑点;
   涨停买单 / 跌停卖单不成交;停牌(当日无 K 线)不成交 ——
   因每日重发目标权重,顺延自然成立,无须专门状态。
   同日**先卖后买**(卖出先回笼现金),买单按目标权重从大到小成交。
2. 强制事件:转债强赎末日(cb_events.last_trade_day ≤ t → 'force_redeem')
   与退市(instruments.delist_date ≤ t → 'force_delist'),当日全额平仓。
   强赎末日优先判定(通常早于摘牌日)。强制平仓无视跌停与 T+1 ——
   终结性事件,不出清的头寸将永远悬空。
3. 收盘结算:equity = cash + Σ q_eff × 后复权收盘(停牌 ffill),
   记录 nav 与实际持仓权重。
4. 构造 SignalContext(today=t,数据在接口层物理截断到 t 收盘,无未来函数)
   → get_strategy(cfg.strategy)(ctx, **cfg.params, lookback=cfg.lookback)
   → 目标权重(负数截 0;Σ>1 等比缩到 1)
   → 与当前权重比较,|Δw| < settings.execution.rebalance_band 的标的跳过
   → 生成 t+1 委托。已触发强制事件的标的不再接受买单(防止强平后回补)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
会计口径(既定决策,引擎唯一真理;改动必须同步快照测试 golden)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 持仓记"复权股数" q_eff(float)。买入按**不复权执行价**取整手:
      n_raw = floor(目标金额 / 不复权执行价 / lot_size) × lot_size
      q_eff += n_raw / adj_factor(t)
  卖出反向:q_eff −= n_raw / adj_factor(t)。
- 现金流按不复权执行价 × n_raw 计;市值 = q_eff × 后复权收盘(停牌 ffill)。
  买卖时点两种口径严格相等:
      q_eff × close_adj = (n_raw / f) × (close_raw × f) = n_raw × close_raw
  持有期分红送股经 adj_factor 只增不改地抬升市值 —— 等价于红利再投,
  与仓库老脚本(后复权收益)口径一致。
- 卖出取整手后若剩余不足 1 手,一次性清仓(转债交易规则,统一适用于三类品种);
  目标权重归零的卖单直接全额清仓(含零股),不留永远低于
  rebalance_band、再也卖不掉的碎股尾仓。
- 现金不得透支:买单现金不足时缩量到可负担的整数手
      n_afford = floor(cash / (执行价 × (1 + 佣金率)) / lot_size) × lot_size
- T+1(rule.t_plus=1):当日买入的份额记入 bought_today,当日不可卖。
  每日重发净额单向委托的架构下几乎不触发,但状态必须正确;
  强制平仓不受 T+1 与跌停限制。
- 费用:佣金(双边)+ 印花税(仅卖出)为现金口径,逐笔记入 trades.cost;
  滑点已并入执行价,不重复计费。费率一律走 TradingRule(config/rules.yaml)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import Settings, StrategyCfg, load_rules
from ..live.targets import limit_positions
from ..signal.base import SignalContext, get_strategy
from ..universe.cb_terms import EVT_LAST_TRADE_DAY, build_cb_terms
from ..universe.instruments import resolve_rule
from .costs import trade_cost
from .execution_model import exec_price, fillable, prepare_market
from .result import TRADE_COLS, BacktestResult

_EPS = 1e-9

#: 信号层可见列 —— 只暴露后复权 OHLCV,执行细节列(raw/limit)不进 ctx,
#: 防止策略误用不复权口径造成回测–实盘偏差。
_SIG_COLS = ["open", "high", "low", "close", "volume", "amount"]


@dataclass
class _Order:
    """t 日收盘生成、t+1 执行的调仓委托(现金目标金额口径)。"""

    symbol: str
    side: str        # 'buy' | 'sell'
    amount: float    # 目标金额 = |Δw| × 当日 equity
    target_w: float  # 信号目标权重(买单排序、归零清仓判定用)


def run_backtest(
    store,
    strategy_cfg: StrategyCfg,
    settings: Settings,
    start=None,
    end=None,
    initial_capital: float = 1_000_000.0,
    rules_cfg: dict | None = None,
) -> BacktestResult:
    """在 store 的数据上回测 strategy_cfg 指定的策略,返回 BacktestResult。

    rules_cfg 缺省读 config/rules.yaml;传入覆盖值可做滑点/佣金敏感性实验
    (面板的"高级配置"走这里,不落盘、不影响他处)。
    """
    universe = [str(s) for s in strategy_cfg.universe]
    if not universe:
        raise ValueError("strategy_cfg.universe 为空")
    rules_cfg = rules_cfg if rules_cfg is not None else load_rules()

    # ---- 品种主数据:名称(ST 判定)与退市日 ------------------------------
    names: dict[str, str] = {}
    delist: dict[str, pd.Timestamp] = {}
    inst = store.read_instruments()
    if len(inst):
        for row in inst.itertuples():
            sym = str(row.symbol)
            nm = getattr(row, "name", "")
            names[sym] = "" if nm is None or (isinstance(nm, float) and pd.isna(nm)) else str(nm)
            dd = pd.to_datetime(getattr(row, "delist_date", None), errors="coerce")
            if pd.notna(dd):
                delist[sym] = pd.Timestamp(dd)

    rules = {s: resolve_rule(s, strategy_cfg.sec_type, rules_cfg, name=names.get(s, ""))
             for s in universe}

    # ---- 行情:一次读全历史(信号回放需要 start 之前的 lookback 数据) ----
    bars = store.read_bars(symbols=universe, adjust=True)
    if len(bars) == 0:
        raise ValueError(
            f"回测标的在 store 中无行情数据(universe {len(universe)} 只:"
            f"{universe}); 引擎不触网,请先经 sinan.data.ensure.ensure_bars "
            "补数(app 回测页与 run_backtest CLI 已自动调用)")
    mkt: dict[str, pd.DataFrame] = {
        str(sym): prepare_market(g, rules[str(sym)])
        for sym, g in bars.groupby("symbol") if str(sym) in rules
    }
    sig_data = {s: m[_SIG_COLS] for s, m in mkt.items()}

    # ---- 转债条款与强赎末日 ---------------------------------------------
    events_df = store.read_cb_events(universe)
    cb_terms = build_cb_terms(store.read_cb_terms(universe), events_df)
    last_trade: dict[str, pd.Timestamp] = {}
    if len(events_df) and "event" in events_df.columns:
        sub = events_df[events_df["event"] == EVT_LAST_TRADE_DAY]
        for sym, g in sub.groupby("symbol"):
            last_trade[str(sym)] = pd.Timestamp(pd.to_datetime(g["date"]).min())

    # ---- 交易日序列:优先日历表,缺失时退化为行情日期并集 ----------------
    data_min = min(m.index.min() for m in mkt.values())
    data_max = max(m.index.max() for m in mkt.values())
    t0 = pd.Timestamp(start) if start is not None else data_min
    t1 = pd.Timestamp(end) if end is not None else data_max
    days = pd.DatetimeIndex(store.read_calendar(start=t0, end=t1))
    if len(days) == 0:
        alld = sorted({d for m in mkt.values() for d in m.index})
        days = pd.DatetimeIndex([d for d in alld if t0 <= d <= t1])
    if len(days) == 0:
        raise ValueError("回测区间内无交易日")

    # 停牌 ffill 序列:结算与(无 K 线日的)强平定价用
    close_adj_ff = pd.DataFrame({s: m["close"].reindex(days, method="ffill") for s, m in mkt.items()})
    close_raw_ff = pd.DataFrame({s: m["close_raw"].reindex(days, method="ffill") for s, m in mkt.items()})
    factor_ff = pd.DataFrame({s: m["adj_factor"].reindex(days, method="ffill") for s, m in mkt.items()})

    strategy = get_strategy(strategy_cfg.strategy)
    params = dict(strategy_cfg.params)
    # 定投计划起始日以回测窗口起点为准:配置里的 start 锚定影子/实盘的真实
    # 计划(必须写死,滚动窗口下才可复现),回测问的是"该计划在这段历史上
    # 表现如何",起点即窗口首日。想复现影子计划,把回测 start 设为计划日即可。
    if strategy_cfg.strategy == "dca" and "start" in params:
        params["start"] = str(days[0].date())
    price_mode = settings.execution.price_mode
    band = (strategy_cfg.rebalance_band if strategy_cfg.rebalance_band is not None
            else settings.execution.rebalance_band)

    # ---- 状态 ------------------------------------------------------------
    cash = float(initial_capital)
    pos: dict[str, float] = {}          # symbol -> q_eff(复权股数)
    queue: list[_Order] = []            # t−1 收盘生成、t 执行的委托
    trades: list[dict] = []
    nav_vals: list[float] = []
    weight_rows: list[dict] = []
    # 执行层止损/止盈覆盖(settings.execution.stop_loss/take_profit,0=关):
    # avg_cost 记复权均价成本(买入加权,部分卖出不改均价);触发后强制清仓,
    # 并把标的挂入 overlay_block,直至策略自身目标归零一次才允许再入场。
    sl, tp = settings.execution.stop_loss, settings.execution.take_profit
    avg_cost: dict[str, float] = {}     # symbol -> 复权均价成本
    overlay_pending: dict[str, str] = {}   # 触发待执行:symbol -> 'stop_loss'|'take_profit'
    overlay_block: set[str] = set()

    def _record(date, symbol, side, qty, price, cost, reason):
        trades.append(dict(date=date, symbol=symbol, side=side,
                           qty=qty, price=price, cost=cost, reason=reason))

    for t in days:
        bought_today: dict[str, float] = {}   # T+1:当日买入的 q_eff,当日不可卖

        # ---- 1. 执行 t−1 排队委托:先卖后买,买单按目标权重从大到小 ----
        sells = sorted((o for o in queue if o.side == "sell"), key=lambda o: o.symbol)
        buys = sorted((o for o in queue if o.side == "buy"),
                      key=lambda o: (-o.target_w, o.symbol))
        queue = []
        for o in sells + buys:
            s, rule, m = o.symbol, rules[o.symbol], mkt.get(o.symbol)
            if m is None or t not in m.index:
                continue                      # 停牌/未上市:不成交,目标次日重发
            row = m.loc[t]
            px = exec_price(row, rule, o.side, price_mode)
            if not fillable(o.side, px, row["up_limit"], row["down_limit"]):
                continue                      # 一字涨停买不进 / 跌停卖不出
            factor = float(row["adj_factor"])

            if o.side == "buy":
                lots = math.floor(o.amount / px / rule.lot_size)
                n_raw = lots * rule.lot_size
                # 现金不得透支:缩量到可负担的整数手(佣金一并预留)
                afford = math.floor(cash / (px * (1 + rule.commission)) / rule.lot_size) \
                    * rule.lot_size
                n_raw = min(n_raw, afford)
                if n_raw <= 0:
                    continue
                gross = n_raw * px
                fee = trade_cost("buy", gross, rule)
                cash -= gross + fee
                dq = n_raw / factor
                q_old = pos.get(s, 0.0)
                # 复权均价成本:加权平均(px×factor 即本笔的复权买入价)
                avg_cost[s] = ((avg_cost.get(s, 0.0) * q_old + px * factor * dq)
                               / (q_old + dq))
                pos[s] = q_old + dq
                if rule.t_plus:
                    bought_today[s] = bought_today.get(s, 0.0) + dq
                _record(t, s, "buy", n_raw, px, fee, "signal")
            else:
                sellable_q = pos.get(s, 0.0)
                if rule.t_plus:
                    sellable_q -= bought_today.get(s, 0.0)
                if sellable_q <= _EPS:
                    continue
                n_equiv = sellable_q * factor          # 当前口径下的不复权股数
                if o.target_w <= _EPS:
                    n_raw = n_equiv                    # 目标归零:全额清仓(含零股)
                else:
                    lots = min(math.floor(o.amount / px / rule.lot_size),
                               math.floor(n_equiv / rule.lot_size))
                    n_raw = lots * rule.lot_size
                    rest = n_equiv - n_raw
                    if _EPS < rest < rule.lot_size:    # 剩余不足 1 手 → 一并清掉
                        n_raw = n_equiv
                if n_raw <= _EPS:
                    continue
                gross = n_raw * px
                fee = trade_cost("sell", gross, rule)
                cash += gross - fee
                dq = sellable_q if n_raw >= n_equiv - _EPS else n_raw / factor
                pos[s] = pos.get(s, 0.0) - dq
                full_out = pos[s] <= _EPS
                if full_out:
                    pos.pop(s)
                    avg_cost.pop(s, None)
                # 覆盖层触发的清仓:以真实原因记流水(部分成交则保留待执行)
                reason = "signal"
                if s in overlay_pending and full_out:
                    reason = overlay_pending.pop(s)
                _record(t, s, "sell", n_raw, px, fee, reason)

        # ---- 2. 强制事件:强赎末日 / 退市 → 当日全额平仓 -----------------
        for s in list(pos):
            lt, dl = last_trade.get(s), delist.get(s)
            if lt is not None and lt <= t:
                reason = "force_redeem"
            elif dl is not None and dl <= t:
                reason = "force_delist"
            else:
                continue
            rule, m = rules[s], mkt[s]
            if t in m.index:
                row = m.loc[t]
                px = exec_price(row, rule, "sell", price_mode)
                factor = float(row["adj_factor"])
            else:                                # 已无 K 线:用最后可得收盘 ffill
                base = close_raw_ff.at[t, s]
                factor = factor_ff.at[t, s]
                base = 0.0 if pd.isna(base) else float(base)
                factor = 1.0 if pd.isna(factor) else float(factor)
                px = base * (1 - rule.slippage)
            q = pos.pop(s)
            avg_cost.pop(s, None)
            overlay_pending.pop(s, None)
            n_raw = q * factor
            gross = n_raw * px
            fee = trade_cost("sell", gross, rule)
            cash += gross - fee                  # 无视跌停与 T+1(见模块 docstring)
            _record(t, s, "sell", n_raw, px, fee, reason)

        # ---- 3. 收盘结算:equity = cash + Σ q_eff × 后复权收盘(ffill) ----
        pos_val: dict[str, float] = {}
        for s, q in pos.items():
            v = close_adj_ff.at[t, s]
            pos_val[s] = q * (0.0 if pd.isna(v) else float(v))
        equity = cash + sum(pos_val.values())
        nav_vals.append(equity / initial_capital)
        w_row = {s: v / equity for s, v in pos_val.items()}
        weight_rows.append(w_row)

        # ---- 3.5 执行层止损/止盈覆盖:按复权均价成本的浮动收益判定 ----
        if sl > 0 or tp > 0:
            for s, q in pos.items():
                if s in overlay_pending or s not in avg_cost or q <= _EPS:
                    continue
                c = close_adj_ff.at[t, s]
                if pd.isna(c) or avg_cost[s] <= 0:
                    continue
                ret = float(c) / avg_cost[s] - 1.0
                if sl > 0 and ret <= -sl:
                    overlay_pending[s] = "stop_loss"
                    overlay_block.add(s)
                elif tp > 0 and ret >= tp:
                    overlay_pending[s] = "take_profit"
                    overlay_block.add(s)

        # ---- 4. 信号 → 目标权重 → t+1 委托 -------------------------------
        ctx = SignalContext(today=t, data=sig_data, positions=w_row,
                            total_asset=equity, rules=rules, universe=universe,
                            cb_terms=cb_terms)
        raw_targets = strategy(ctx, **params,
                               lookback=strategy_cfg.lookback) or {}
        tgt: dict[str, float] = {}
        for s, w in raw_targets.items():
            s = str(s)
            if s not in rules:
                continue                          # 引擎不信任信号层:池外标的丢弃
            w = float(w)
            tgt[s] = w if np.isfinite(w) and w > 0 else 0.0   # 负数/NaN 截 0
        # 止损/止盈覆盖:已触发的标的强制目标 0(清仓 + 禁回补),
        # 直至策略自身目标归零一次(且仓位已出清)才解除——防止次日回补
        for s in list(overlay_block):
            if tgt.get(s, 0.0) <= _EPS and s not in pos:
                overlay_block.discard(s)
            else:
                tgt[s] = 0.0

        # 持仓数上限(settings.risk.max_positions,0=不限):与实盘 apply_risk
        # 同一实现,已持仓优先、新入场按权重排序 —— 先裁"谁在场",再缩权重
        tgt, _ = limit_positions(tgt, w_row, settings.risk.max_positions)
        total = sum(tgt.values())
        if total > 1.0 + _EPS:                    # Σ>1 等比缩到满仓
            tgt = {s: w / total for s, w in tgt.items()}

        blocked = {s for s in universe
                   if (last_trade.get(s) is not None and last_trade[s] <= t)
                   or (delist.get(s) is not None and delist[s] <= t)}
        for s in sorted(set(tgt) | set(w_row)):
            dw = tgt.get(s, 0.0) - w_row.get(s, 0.0)
            if abs(dw) < band:                    # 再平衡带宽:小偏差不磨损成本
                continue
            if dw > 0:
                if s in blocked:                  # 强制事件后不再开仓
                    continue
                queue.append(_Order(s, "buy", dw * equity, tgt.get(s, 0.0)))
            else:
                queue.append(_Order(s, "sell", -dw * equity, tgt.get(s, 0.0)))

    # ---- 组装结果 --------------------------------------------------------
    nav = pd.Series(nav_vals, index=days, name="nav")
    returns = nav.pct_change()
    returns.iloc[0] = nav.iloc[0] - 1.0           # 首日相对初始净值 1.0
    weights = (pd.DataFrame(weight_rows, index=days)
               .reindex(columns=universe).fillna(0.0))
    trades_df = pd.DataFrame(trades, columns=TRADE_COLS)
    meta = {
        "strategy": strategy_cfg.strategy,
        "name": strategy_cfg.name,
        "params": dict(params),        # 实际生效参数(dca 的 start 已对齐窗口起点)
        "lookback": strategy_cfg.lookback,
        "universe": universe,
        "sec_type": strategy_cfg.sec_type,
        "start": str(days[0].date()),
        "end": str(days[-1].date()),
        "initial_capital": float(initial_capital),
        "price_mode": price_mode,
        "rebalance_band": band,
        "final_cash": cash,
    }
    return BacktestResult(nav=nav, returns=returns, weights=weights,
                          trades=trades_df, meta=meta)
