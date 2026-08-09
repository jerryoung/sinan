"""
配置加载:YAML + pydantic 校验(方案 §3)。

所有路径相对项目根(trend/)解析;策略参数、费率、风控阈值全部配置化,
代码中不硬编码任何可变数值。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# 项目根 = trend/(本文件在 trend/trend/config.py)
ROOT = Path(__file__).resolve().parent.parent

TRADING_DAYS = 244  # A 股年化基准


class ExecutionCfg(BaseModel):
    price_mode: Literal["close", "open"] = "close"
    # 再平衡带宽:|目标权重 − 当前权重| 小于该值不调仓,
    # 避免入场锁定权重与市值漂移之间的日频微调仓磨损成本
    rebalance_band: float = 0.02
    # 执行层止损/止盈覆盖(0 = 关闭):按持仓的复权均价成本计浮动收益,
    # 触发即强制清仓,并阻止再入场直至策略自身目标归零一次(防止次日回补)。
    # 注意:这是"不信任信号层"的兜底,与策略内建止损(2N/吊灯/危险信号)叠加。
    stop_loss: float = 0.0
    take_profit: float = 0.0


class RiskCfg(BaseModel):
    max_weight_per_symbol: float = 0.34
    max_total_weight: float = 1.0
    max_daily_turnover: float = 1.0
    liquidity_pct_adv20: float = 0.05
    targets_max_age_hours: float = 8.0
    max_positions: int = 0        # 同时持仓数上限;0 = 不限(海龟 12-unit 之组合版)


class Settings(BaseModel):
    # 名义本金:影子模式出参考委托单、回测默认初始资金用;实盘接入 QMT 后
    # 以 fills 回报的 total_asset 为准,此值仅作无回报时的兜底
    capital: float = 1_000_000.0
    store_root: Path = Path("var/store")
    targets_dir: Path = Path("var/runtime/targets")
    fills_dir: Path = Path("var/runtime/fills")
    reports_dir: Path = Path("var/reports")
    wecom_webhook: str = ""
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)

    def model_post_init(self, _ctx) -> None:
        # 相对路径统一落到项目根下,保证 cron 从任意 cwd 调用行为一致
        for name in ("store_root", "targets_dir", "fills_dir", "reports_dir"):
            p = getattr(self, name)
            if not p.is_absolute():
                object.__setattr__(self, name, ROOT / p)


class StrategyCfg(BaseModel):
    """一份策略 = 一个 YAML(config/strategies/*.yaml)。"""

    name: str                       # 实例名,用于 targets 留痕
    strategy: str                   # 注册表里的策略函数名,如 "donchian"
    universe: list[str]             # 标的池(6 位代码)
    sec_type: str = "etf"           # universe 的品种类型
    lookback: int = 750             # 信号重放窗口(须覆盖最长持仓周期)
    # 该策略的专属本金(影子模式参考委托单与回测初始资金);
    # None = 用全局 settings.capital。优先级:CLI --total-asset > 此处 > 全局
    capital: float | None = None
    # 再平衡带宽覆盖:None = 用全局 execution.rebalance_band。
    # 定投类策略单期增量小(如 1.3%),须低于默认 2% 带宽才能成交
    rebalance_band: float | None = None
    params: dict = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config" / "settings.yaml"
    return Settings(**_load_yaml(p))


def load_rules(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "rules.yaml"
    return _load_yaml(p)


def load_strategy(path: str | Path) -> StrategyCfg:
    return StrategyCfg(**_load_yaml(Path(path)))
