"""命名实盘配置集合操作:引用扫描、不可变更新与安全删除。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import (LiveProfileCfg, LiveProfilesCfg, load_strategy)


@dataclass(frozen=True)
class StrategyProfileRef:
    strategy_id: str
    display_name: str
    path: Path


class ProfileDeleteBlocked(ValueError):
    """配置仍是默认项、仍被引用或无法安全完成引用扫描。"""

    def __init__(
        self,
        message: str,
        *,
        references: list[StrategyProfileRef] | None = None,
        parse_errors: list[Path] | None = None,
    ) -> None:
        super().__init__(message)
        self.references = list(references or [])
        self.parse_errors = list(parse_errors or [])


def _scan_references(
    profile_id: str,
    strategy_dir: Path,
) -> tuple[list[StrategyProfileRef], list[Path]]:
    refs: list[StrategyProfileRef] = []
    errors: list[Path] = []
    for path in sorted(Path(strategy_dir).glob("*.yaml")):
        try:
            cfg = load_strategy(path)
        except Exception:  # noqa: BLE001 删除必须 fail-closed,具体文件交 UI 展示
            errors.append(path)
            continue
        if cfg.live_profile == profile_id:
            refs.append(StrategyProfileRef(
                strategy_id=cfg.name,
                display_name=cfg.display_name or cfg.name,
                path=path,
            ))
    return refs, errors


def find_profile_references(
    profile_id: str,
    strategy_dir: Path,
) -> list[StrategyProfileRef]:
    """返回引用 profile_id 的策略;有坏 YAML 时拒绝给出不完整结论。"""
    refs, errors = _scan_references(profile_id, Path(strategy_dir))
    if errors:
        raise ProfileDeleteBlocked(
            "存在无法解析的策略配置,引用扫描不完整",
            references=refs,
            parse_errors=errors,
        )
    return refs


def upsert_live_profile(
    cfg: LiveProfilesCfg,
    profile_id: str,
    profile: LiveProfileCfg,
) -> LiveProfilesCfg:
    """新增或编辑配置,返回重新校验后的新集合。"""
    data = cfg.model_dump(mode="python")
    data["profiles"][profile_id] = profile.model_dump(mode="python")
    return LiveProfilesCfg(**data)


def set_default_live_profile(
    cfg: LiveProfilesCfg,
    profile_id: str,
) -> LiveProfilesCfg:
    """切换默认配置;目标必须已存在。"""
    if profile_id not in cfg.profiles:
        raise ValueError(f"实盘配置不存在:{profile_id}")
    data = cfg.model_dump(mode="python")
    data["default"] = profile_id
    return LiveProfilesCfg(**data)


def delete_live_profile(
    cfg: LiveProfilesCfg,
    profile_id: str,
    strategy_dir: Path,
) -> LiveProfilesCfg:
    """删除非默认、未引用配置;扫描不完整时不做任何修改。"""
    if profile_id not in cfg.profiles:
        raise ValueError(f"实盘配置不存在:{profile_id}")
    if profile_id == cfg.default:
        raise ProfileDeleteBlocked("默认实盘配置禁止删除,请先切换默认配置")

    refs, errors = _scan_references(profile_id, Path(strategy_dir))
    if errors:
        raise ProfileDeleteBlocked(
            "存在无法解析的策略配置,无法安全删除",
            references=refs,
            parse_errors=errors,
        )
    if refs:
        raise ProfileDeleteBlocked(
            f"实盘配置仍被 {len(refs)} 个策略引用,禁止删除",
            references=refs,
        )

    data = cfg.model_dump(mode="python")
    del data["profiles"][profile_id]
    return LiveProfilesCfg(**data)
