"""命名实盘配置集合操作:引用扫描、不可变更新与 fail-closed 删除。"""
from pathlib import Path

import pytest

from sinan.config import LiveProfileCfg, LiveProfilesCfg
from sinan.live.profiles import (
    ProfileDeleteBlocked,
    delete_live_profile,
    find_profile_references,
    set_default_live_profile,
    upsert_live_profile,
)


def _profile(name: str) -> LiveProfileCfg:
    return LiveProfileCfg(name=name, engine="qmt")


def _profiles() -> LiveProfilesCfg:
    return LiveProfilesCfg(
        default="local_qmt",
        profiles={
            "local_qmt": _profile("本地 QMT"),
            "paper_qmt": _profile("模拟 QMT"),
        },
    )


def _write_strategy(path: Path, strategy_id: str, live_profile: str) -> None:
    path.write_text(
        f"name: {strategy_id}\n"
        f"display_name: {strategy_id}展示\n"
        "strategy: donchian\n"
        "universe: ['510300']\n"
        f"live_profile: {live_profile}\n",
        encoding="utf-8",
    )


def test_find_profile_references_reports_identity_and_file(tmp_path):
    _write_strategy(tmp_path / "alpha.yaml", "alpha", "paper_qmt")
    _write_strategy(tmp_path / "beta.yaml", "beta", "local_qmt")
    refs = find_profile_references("paper_qmt", tmp_path)
    assert [(r.strategy_id, r.display_name, r.path.name) for r in refs] == [
        ("alpha", "alpha展示", "alpha.yaml")
    ]


def test_default_profile_cannot_be_deleted(tmp_path):
    with pytest.raises(ProfileDeleteBlocked, match="默认") as exc:
        delete_live_profile(_profiles(), "local_qmt", tmp_path)
    assert exc.value.references == []
    assert exc.value.parse_errors == []


def test_referenced_profile_cannot_be_deleted_and_lists_strategy(tmp_path):
    _write_strategy(tmp_path / "alpha.yaml", "alpha", "paper_qmt")
    with pytest.raises(ProfileDeleteBlocked, match="引用") as exc:
        delete_live_profile(_profiles(), "paper_qmt", tmp_path)
    assert exc.value.references[0].strategy_id == "alpha"
    assert exc.value.references[0].path.name == "alpha.yaml"


def test_unparseable_strategy_blocks_delete(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [", encoding="utf-8")
    with pytest.raises(ProfileDeleteBlocked, match="无法解析") as exc:
        delete_live_profile(_profiles(), "paper_qmt", tmp_path)
    assert exc.value.parse_errors == [tmp_path / "broken.yaml"]


def test_unreferenced_non_default_profile_can_be_deleted_without_mutating_input(tmp_path):
    before = _profiles()
    out = delete_live_profile(before, "paper_qmt", tmp_path)
    assert "paper_qmt" not in out.profiles
    assert "paper_qmt" in before.profiles


def test_delete_unknown_profile_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        delete_live_profile(_profiles(), "missing", tmp_path)


def test_upsert_adds_and_edits_without_mutating_input():
    before = _profiles()
    added = upsert_live_profile(before, "remote_qmt", _profile("远端 QMT"))
    assert added.profiles["remote_qmt"].name == "远端 QMT"
    assert "remote_qmt" not in before.profiles

    edited = upsert_live_profile(added, "remote_qmt", _profile("远端 QMT 2"))
    assert edited.profiles["remote_qmt"].name == "远端 QMT 2"
    assert added.profiles["remote_qmt"].name == "远端 QMT"


def test_upsert_reuses_model_id_validation():
    with pytest.raises(ValueError, match="配置 ID"):
        upsert_live_profile(_profiles(), "Remote QMT", _profile("远端"))


def test_set_default_requires_existing_profile_and_is_immutable():
    before = _profiles()
    out = set_default_live_profile(before, "paper_qmt")
    assert out.default == "paper_qmt"
    assert before.default == "local_qmt"
    with pytest.raises(ValueError, match="不存在"):
        set_default_live_profile(before, "missing")
