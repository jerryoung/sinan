"""
配置加载:YAML + pydantic 校验(方案 §3)。

所有路径相对项目根(trend/)解析;策略参数、费率、风控阈值全部配置化,
代码中不硬编码任何可变数值。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import copy

import yaml
from pydantic import (BaseModel, ConfigDict, Field, field_validator,
                      model_validator)

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
    # 对账容忍度(权重绝对差):次日出信号时比对上一执行日的 targets vs fills。
    # 不能设太紧——T−1 收盘到 T 收盘的价格漂移本身就会造成权重偏差
    # (20% 的仓位当天涨 5% ≈ 1pp),那不是执行失败。仅告警,不阻断。
    reconcile_tolerance: float = 0.02


class QmtAlgoCfg(BaseModel):
    """QMT 下单算法参数;命名实盘配置是唯一编辑入口。"""

    model_config = ConfigDict(extra="forbid")

    quote_mode: Literal["latest", "limit"] = "latest"
    price_offset: float = Field(default=0.002, ge=0, allow_inf_nan=False)
    max_order_qty: int = Field(default=10000, gt=0)


class QmtRpcCfg(BaseModel):
    """QMT 数据/RPC 连接参数;机密 token 仍只存用户目录。"""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=58620, ge=1, le=65535)
    timeout: float = Field(default=15.0, gt=0, allow_inf_nan=False)

    @field_validator("host")
    @classmethod
    def _non_empty_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host 不能为空")
        return value


class QmtExecutionCfg(BaseModel):
    """QMT 执行参数。

    account 当前仅随 targets 留痕,薄壳仍按 QMT 模型绑定账号下单;保留该键
    是为未来多账号路由扩展,界面必须如实提示这一点。
    """

    model_config = ConfigDict(extra="forbid")

    account: str | None = None
    rpc: QmtRpcCfg = Field(default_factory=QmtRpcCfg)
    algo: QmtAlgoCfg = Field(default_factory=QmtAlgoCfg)

    @field_validator("account", mode="before")
    @classmethod
    def _blank_account_as_none(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def targets_payload(self) -> dict:
        """生成 QMT 薄壳执行字段;RPC 网络参数不进入 targets 文件。"""
        return self.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"rpc"},
        )


class LiveProfileCfg(BaseModel):
    """一份可被多个策略引用的实盘配置。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    engine: Literal["qmt"] = "qmt"
    qmt: QmtExecutionCfg = Field(default_factory=QmtExecutionCfg)

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name 不能为空")
        return value


class LiveProfilesCfg(BaseModel):
    """config/live_profiles.yaml 的完整内容。"""

    model_config = ConfigDict(extra="forbid")

    default: str
    profiles: dict[str, LiveProfileCfg]

    @model_validator(mode="after")
    def _validate_ids_and_default(self):
        if not self.profiles:
            raise ValueError("实盘配置不能为空")
        for profile_id in self.profiles:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", profile_id):
                raise ValueError(f"实盘配置 ID 不合法:{profile_id!r}")
        if self.default not in self.profiles:
            raise ValueError(f"default 指向不存在的实盘配置:{self.default}")
        return self


class DataCfg(BaseModel):
    """数据源链配置(日更 run_daily 的 [主源, 备源, ...] 顺序)。

    名字对应 sinan/data/sources/ 注册表里的适配器(@register_source);
    加源/换源/调顺序只改这里,不动调用方代码。单个源在当前环境不可用
    (token 缺失、非 QMT 机器等)由 build_sources 降级跳过,全部不可用才报错。
    """
    sources: list[str] = Field(default_factory=lambda: ["akshare", "tushare"])

    @field_validator("sources", mode="before")
    @classmethod
    def _none_as_default(cls, v):
        return ["akshare", "tushare"] if v is None else v

    @field_validator("sources")
    @classmethod
    def _normalize_source_chain(cls, values: list[str]) -> list[str]:
        """源链是有序回退契约:至少一项、名称规范化、不得重复。"""
        if not values:
            raise ValueError("至少配置一个数据源")
        normalized: list[str] = []
        for value in values:
            source = value.strip().lower()
            if not source:
                raise ValueError("数据源名称不能为空")
            if source in normalized:
                raise ValueError(f"数据源不能重复:{source}")
            normalized.append(source)
        return normalized


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    data: DataCfg = Field(default_factory=DataCfg)

    @field_validator("data", mode="before")
    @classmethod
    def _data_none_as_default(cls, v):
        return {} if v is None else v

    def model_post_init(self, _ctx) -> None:
        # 相对路径统一落到项目根下,保证 cron 从任意 cwd 调用行为一致
        for name in ("store_root", "targets_dir", "fills_dir", "reports_dir"):
            p = getattr(self, name)
            if not p.is_absolute():
                object.__setattr__(self, name, ROOT / p)


class StrategyCfg(BaseModel):
    """一份策略 = 一个 YAML(config/strategies/*.yaml)。"""

    model_config = ConfigDict(extra="forbid")

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
    # 命名实盘配置引用(config/live_profiles.yaml);仓库策略 YAML 显式写出,
    # 模型默认值供测试/程序化创建策略时使用。引用不存在时出信号直接拒绝。
    live_profile: str = "local_qmt"
    params: dict = Field(default_factory=dict)


def resolve_live_profile(
    profiles: LiveProfilesCfg,
    cfg: StrategyCfg,
) -> tuple[str, LiveProfileCfg]:
    """解析策略的命名实盘配置;悬空引用直接拒绝,不回退默认配置。"""
    profile_id = str(cfg.live_profile or "").strip()
    if not profile_id or profile_id not in profiles.profiles:
        raise ValueError(f"策略 {cfg.name} 引用了不存在的实盘配置:{profile_id!r}")
    return profile_id, copy.deepcopy(profiles.profiles[profile_id])


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML 映射重复键必须报错,避免同名配置被后一个静默覆盖。"""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"YAML 重复键:{key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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


def load_live_profiles(path: str | Path | None = None) -> LiveProfilesCfg:
    """加载命名实盘配置;相比普通 YAML 加载额外拒绝重复键。"""
    p = Path(path) if path else ROOT / "config" / "live_profiles.yaml"
    with open(p, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_UniqueKeyLoader) or {}
    return LiveProfilesCfg(**data)


def save_live_profiles(
    cfg: LiveProfilesCfg,
    path: str | Path | None = None,
) -> Path:
    """原子保存命名实盘配置;失败时原文件保持不变。"""
    p = Path(path) if path else ROOT / "config" / "live_profiles.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = yaml.safe_dump(
        cfg.model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return p
