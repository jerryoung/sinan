"""命名实盘配置契约:强类型模型、唯一键 YAML、原子保存与策略解析。"""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sinan.config import (
    LiveProfileCfg,
    LiveProfilesCfg,
    QmtAlgoCfg,
    QmtExecutionCfg,
    ROOT,
    Settings,
    StrategyCfg,
    load_live_profiles,
    load_strategy,
    resolve_live_profile,
    save_live_profiles,
)


def _profile(name: str = "本地 QMT", **qmt) -> LiveProfileCfg:
    return LiveProfileCfg(name=name, engine="qmt", qmt=qmt or {})


def _profiles() -> LiveProfilesCfg:
    return LiveProfilesCfg(
        default="local_qmt",
        profiles={
            "local_qmt": LiveProfileCfg(
                name="本地 QMT",
                engine="qmt",
                qmt=QmtExecutionCfg(
                    algo=QmtAlgoCfg(
                        quote_mode="latest",
                        price_offset=0.002,
                        max_order_qty=10000,
                    )
                ),
            )
        },
    )


def _strategy(**kw) -> StrategyCfg:
    data = dict(name="s", strategy="donchian", universe=["510300"])
    data.update(kw)
    return StrategyCfg(**data)


def test_repo_live_profiles_has_local_qmt():
    cfg = load_live_profiles()
    assert cfg.default == "local_qmt"
    assert cfg.profiles["local_qmt"].engine == "qmt"
    assert cfg.profiles["local_qmt"].qmt.algo.quote_mode == "latest"
    assert cfg.profiles["local_qmt"].qmt.algo.price_offset == pytest.approx(0.002)
    assert cfg.profiles["local_qmt"].qmt.algo.max_order_qty == 10000


def test_all_repo_strategies_explicitly_reference_existing_profile():
    profiles = load_live_profiles()
    for path in sorted((ROOT / "config" / "strategies").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert raw.get("live_profile"), path.name
        assert "qmt" not in raw, path.name
        resolve_live_profile(profiles, load_strategy(path))


def test_settings_no_longer_has_inline_live_qmt():
    assert "live" not in Settings.model_fields
    assert "qmt_rpc" in Settings.model_fields
    with pytest.raises(ValidationError, match="live"):
        Settings(live={"engine": "qmt", "qmt": {}})


def test_repo_settings_has_no_legacy_live_section_and_keeps_qmt_rpc():
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert "live" not in raw
    assert "qmt_rpc" in raw


def test_production_entrypoints_do_not_use_legacy_qmt_resolver():
    for relative in ("scripts/run_signal.py", "ui/common.py", "ui/settings_page.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "resolve_qmt" not in source, relative
        assert "Settings.live" not in source, relative


def test_strategy_defaults_to_local_profile_and_rejects_legacy_qmt():
    assert _strategy().live_profile == "local_qmt"
    with pytest.raises(ValidationError, match="qmt"):
        _strategy(qmt={"algo": {"quote_mode": "limit"}})


@pytest.mark.parametrize("profile_id", ["LocalQmt", "local qmt", "1qmt", "本地qmt", ""])
def test_profile_id_is_restricted(profile_id):
    with pytest.raises(ValidationError, match="配置 ID"):
        LiveProfilesCfg(default=profile_id, profiles={profile_id: _profile()})


def test_empty_profiles_and_missing_default_are_rejected():
    with pytest.raises(ValidationError, match="不能为空"):
        LiveProfilesCfg(default="local_qmt", profiles={})
    with pytest.raises(ValidationError, match="default.*不存在"):
        LiveProfilesCfg(default="missing", profiles={"local_qmt": _profile()})


def test_profile_name_is_trimmed_and_must_not_be_empty():
    assert _profile("  本地 QMT  ").name == "本地 QMT"
    with pytest.raises(ValidationError, match="name"):
        _profile("   ")


@pytest.mark.parametrize(
    "algo,field",
    [
        ({"quote_mode": "lastest"}, "quote_mode"),
        ({"price_offset": -0.1}, "price_offset"),
        ({"price_offset": float("nan")}, "price_offset"),
        ({"max_order_qty": 0}, "max_order_qty"),
    ],
)
def test_qmt_algo_rejects_invalid_values(algo, field):
    with pytest.raises(ValidationError, match=field):
        QmtAlgoCfg(**algo)


def test_unknown_engine_and_unknown_qmt_keys_are_rejected():
    with pytest.raises(ValidationError, match="engine"):
        LiveProfileCfg(name="P", engine="ptrade")
    with pytest.raises(ValidationError, match="unknown"):
        QmtExecutionCfg(unknown=True)


def test_duplicate_yaml_profile_id_is_rejected(tmp_path):
    path = tmp_path / "live_profiles.yaml"
    path.write_text(
        "default: a\nprofiles:\n"
        "  a: {name: A, engine: qmt}\n"
        "  a: {name: B, engine: qmt}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复键.*a"):
        load_live_profiles(path)


def test_strategy_reference_must_exist_without_default_fallback():
    cfg = _strategy(live_profile="missing")
    with pytest.raises(ValueError, match="策略 s.*missing"):
        resolve_live_profile(_profiles(), cfg)


def test_resolver_returns_profile_id_and_deep_copy():
    profiles = _profiles()
    profile_id, resolved = resolve_live_profile(profiles, _strategy())
    assert profile_id == "local_qmt"
    resolved.qmt.algo.price_offset = 9.9
    assert profiles.profiles["local_qmt"].qmt.algo.price_offset == pytest.approx(0.002)


def test_resolved_profile_serializes_for_existing_qmt_shell_contract():
    profile_id, profile = resolve_live_profile(_profiles(), _strategy())
    qmt = profile.qmt.model_dump(mode="json", exclude_none=True)
    assert profile_id == "local_qmt"
    assert qmt == {
        "algo": {
            "quote_mode": "latest",
            "price_offset": 0.002,
            "max_order_qty": 10000,
        }
    }


def test_profile_yaml_roundtrip_and_atomic_save(tmp_path):
    path = tmp_path / "live_profiles.yaml"
    out = save_live_profiles(_profiles(), path)
    assert out == path
    assert load_live_profiles(path) == _profiles()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_failed_atomic_replace_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "live_profiles.yaml"
    path.write_text("original\n", encoding="utf-8")
    original_replace = Path.replace

    def fail_replace(self, target):
        if self.name.endswith(".tmp"):
            raise OSError("disk failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        save_live_profiles(_profiles(), path)
    assert path.read_text(encoding="utf-8") == "original\n"
    assert not path.with_suffix(path.suffix + ".tmp").exists()
