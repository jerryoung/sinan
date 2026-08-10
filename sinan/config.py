"""
配置加载:YAML + pydantic 校验(方案 §3)。

所有路径相对项目根(trend/)解析;策略参数、费率、风控阈值全部配置化,
代码中不硬编码任何可变数值。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import copy

import yaml
from pydantic import BaseModel, Field, field_validator

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


class LiveCfg(BaseModel):
    """实盘设置(系统级缺省)。

    engine: 默认实盘引擎。当前仅 qmt(QMT 薄壳文件桥接);字段本身是
        扩展缝——未来接入其他券商/柜台时在此登记并按值派发。
    qmt: 全局 QMT 执行配置,与策略级 StrategyCfg.qmt 同构(account/algo)。
        sinan 不解释其语义、额外键原样透传薄壳,但 algo 的三个约定键要做
        类型校验(见 check_qmt)——坏值留到 14:45 才炸会瘫痪当日全部策略。
        策略未配置 qmt 时整体生效;策略配置了则整体让位,见 resolve_qmt。
    """
    engine: Literal["qmt"] = "qmt"
    qmt: dict = Field(default_factory=dict)

    @field_validator("qmt", mode="before")
    @classmethod
    def _none_as_empty(cls, v):
        # YAML 里写 `qmt:`(无值)解析为 None:按"未配置"处理而不是崩掉加载,
        # 否则一个手滑的空行会让 run_signal/nightly/面板全线不可用
        return {} if v is None else v


class Settings(BaseModel):
    # 名义本金:影子模式出参考委托单、回测默认初始资金用;实盘接入 QMT 后
    # 以 fills 回报的 total_asset 为准,此值仅作无回报时的兜底
    capital: float = 1_000_000.0
    store_root: Path = Path("var/store")
    targets_dir: Path = Path("var/runtime/targets")
    fills_dir: Path = Path("var/runtime/fills")
    reports_dir: Path = Path("var/reports")
    wecom_webhook: str = ""
    # 远端 QMT rpc_server 连接参数(host/port/timeout,非机密);
    # token 存 ~/.qmt_rpc_token(机密,严禁入库),qmt_sdk.connect_from_settings 读取
    qmt_rpc: dict = Field(default_factory=dict)
    live: LiveCfg = Field(default_factory=LiveCfg)

    @field_validator("live", mode="before")
    @classmethod
    def _live_none_as_default(cls, v):
        return {} if v is None else v

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
    # 展示名(纯 UI 用途,不参与 targets/报告等任何留痕契约);
    # None = 界面回退显示配置文件名
    display_name: str | None = None
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
    # QMT 执行配置(可选):原样嵌入 targets payload 的 "qmt" 字段供薄壳读取,
    # sinan 不解释其内容——每个策略可绑定不同账号与下单算法。约定键:
    #   account: 资金账号(当前薄壳按 QMT 绑定账号下单,此键仅随 targets
    #            留痕,多账号扩展预留;推荐留空)
    #   algo: {quote_mode: latest|limit, price_offset: 0.002,
    #          max_order_qty: 10000}  # 报价方式/限价偏移/单笔拆单上限
    # None = 用全局 settings.live.qmt;配置了则整体覆盖全局(见 resolve_qmt)
    qmt: dict | None = None
    params: dict = Field(default_factory=dict)


# algo 的三个约定键与其类型转换器;额外键不校验、原样透传薄壳
_ALGO_TYPES = {"quote_mode": str, "price_offset": float, "max_order_qty": int}
_QUOTE_MODES = ("latest", "limit")


def check_qmt(qmt: dict, *, source: str) -> None:
    """对 qmt.algo 的约定键做宽校验,坏值在出 targets 时就拒绝。

    "sinan 不解释 qmt 内容"是指不干预语义(额外键照单全收),不等于放任
    类型错误穿透:薄壳 do_rebalance 对 float(price_offset) 无逐策略兜底,
    一个 "0.2%" 会让 14:45 当日**全部**策略的调仓中断。宁可现在拒绝生成,
    也不要空仓之外的意外——与 targets 时效校验同一条原则。
    """
    algo = qmt.get("algo")
    if algo is None:
        return
    if not isinstance(algo, dict):
        raise ValueError(f"{source} 的 qmt.algo 必须是字典,得到 {type(algo).__name__}")
    for key, caster in _ALGO_TYPES.items():
        if key not in algo:
            continue
        try:
            caster(algo[key])
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"{source} 的 qmt.algo.{key} 无法解析为 {caster.__name__}: "
                f"{algo[key]!r}") from e
    mode = algo.get("quote_mode")
    if mode is not None and str(mode) not in _QUOTE_MODES:
        raise ValueError(
            f"{source} 的 qmt.algo.quote_mode 未知取值 {mode!r},"
            f"可选 {'/'.join(_QUOTE_MODES)}(薄壳会静默落回市价单)")


def resolve_qmt(settings: Settings, cfg: StrategyCfg) -> dict | None:
    """QMT 执行配置解析:策略级整体覆盖全局(不做键级合并)。

    策略 YAML 配了非空 qmt → 用策略的——不与全局拼接,避免"账号来自
    全局、算法来自策略"的隐式组合在下单场景造成误判;未配置(None)或
    显式空 dict → 用全局 settings.live.qmt(系统设置·实盘设置);
    两者皆空 → None,targets 不写 qmt 字段,薄壳退回其内置缺省
    (运行环境账号 + ALGO_DEFAULT)。

    注:`qmt: {}` 与不写该键等价(都表示"没有策略级配置"),策略没有
    "屏蔽全局、强制用薄壳缺省"这一档——需要时把全局配置清空即可。
    返回深拷贝:调用方对结果(含嵌套 algo)的任何改动都不回写配置对象。
    """
    if cfg.qmt:
        check_qmt(cfg.qmt, source=f"策略 {cfg.name}")
        return copy.deepcopy(cfg.qmt)
    if settings.live.qmt:
        check_qmt(settings.live.qmt, source="全局实盘设置 live.qmt")
        return copy.deepcopy(settings.live.qmt)
    return None


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
