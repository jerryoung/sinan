"""targets 经 QMT RPC 显式发布：服务端校验、幂等和本地桥接契约。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from qmt_shell import sinan_qmt as rpc_server
from sinan.config import LiveProfileCfg, LiveProfilesCfg, QmtRpcCfg
from sinan.live.qmt_bridge import PublishResult, QmtRpcBridge
from sinan.live.targets import targets_checksum


def _payload(strategy="alpha", date="2026-08-21", targets=None, **extra):
    targets = targets or {"510300": 0.5}
    payload = {
        "date": date,
        "generated_at": "2026-08-21T14:35:00",
        "data_cutoff": "2026-08-20",
        "strategy": strategy,
        "params_fingerprint": "abc123",
        "targets": targets,
        "checksum": targets_checksum(targets),
    }
    payload.update(extra)
    return payload


def test_publish_targets_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    payload = _payload()

    first = rpc_server._publish_targets(payload)
    second = rpc_server._publish_targets(payload)

    assert first == {
        "status": "accepted", "strategy": "alpha", "date": "2026-08-21",
        "checksum": payload["checksum"],
        "filename": "targets_alpha_20260821.json",
    }
    assert second["status"] == "duplicate"
    files = list((tmp_path / "targets").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == payload


def test_publish_targets_replaces_unstarted_payload_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    first = _payload(targets={"510300": 0.5})
    second = _payload(targets={"510300": 0.4})

    rpc_server._publish_targets(first)
    result = rpc_server._publish_targets(second)

    assert result["status"] == "replaced"
    stored = json.loads(
        (tmp_path / "targets" / result["filename"]).read_text(encoding="utf-8")
    )
    assert stored == second
    assert not list((tmp_path / "targets").glob("*.tmp"))


@pytest.mark.parametrize("strategy", [
    "", "../alpha", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b',
    "a<b", "a>b", "a|b", "a\nb", "a\x00b", "alpha ", "a" * 129,
])
def test_publish_targets_rejects_unsafe_strategy(strategy, tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="策略"):
        rpc_server._publish_targets(_payload(strategy=strategy))

    assert not (tmp_path / "targets").exists()


@pytest.mark.parametrize("day", ["20260821", "2026-8-21", "2026-02-30", "../x"])
def test_publish_targets_rejects_invalid_date(day, tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="日期"):
        rpc_server._publish_targets(_payload(date=day))


def test_publish_targets_rejects_checksum_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="checksum"):
        rpc_server._publish_targets(_payload(checksum="0" * 64))


@pytest.mark.parametrize("targets", [
    {"510300": -0.01}, {"510300": 1.01},
    {"510300": 0.6, "159915": 0.5},
])
def test_publish_targets_rejects_weights_outside_long_only_budget(
        targets, tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="权重|总权重"):
        rpc_server._publish_targets(_payload(targets=targets))


def test_qmt_loader_rejects_payload_identity_that_does_not_match_filename(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    directory = tmp_path / "targets"
    directory.mkdir()
    (directory / "targets_alpha_20260821.json").write_text(
        json.dumps(_payload(strategy="../escape")), encoding="utf-8"
    )

    assert rpc_server._load_today_targets(datetime(2026, 8, 21, 14, 45)) == []
    assert "跳过" in capsys.readouterr().out


def test_publish_targets_rejects_oversized_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="过大"):
        rpc_server._publish_targets(_payload(note="x" * (1024 * 1024)))


def test_publish_endpoint_is_local_immediate_and_respects_readonly(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    payload = _payload(filename="../../chosen-by-client.json")

    with pytest.raises(PermissionError, match="只读通道"):
        rpc_server.dispatch({}, object(), "rpc.publish_targets", [payload], {}, False)

    result = rpc_server.dispatch(
        {}, object(), "rpc.publish_targets", [payload], {}, True
    )
    assert result["filename"] == "targets_alpha_20260821.json"
    assert rpc_server._RPC_API_QUEUE.empty()


def test_health_advertises_target_publish_capability():
    health = rpc_server.dispatch({}, object(), "rpc.health", [], {}, True)
    assert "publish_targets" in health["capabilities"]


class _FakeClient:
    instances = []
    responses = {}

    def __init__(self):
        self.connected = None
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    def connect(self, host, port, token, timeout):
        self.connected = (host, port, token, timeout)
        return self

    def call(self, fn, *args):
        self.calls.append((fn, args))
        value = self.responses.get(fn)
        return value(*args) if callable(value) else value

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeClient.instances = []
    _FakeClient.responses = {}


def test_local_bridge_reads_token_and_forwards_exact_payload(tmp_path):
    token = tmp_path / "token"
    token.write_text("  secret-token\n", encoding="utf-8")
    payload = _payload()
    _FakeClient.responses["rpc.publish_targets"] = {
        "status": "accepted", "strategy": "alpha", "date": "2026-08-21",
        "checksum": payload["checksum"], "filename": "targets_alpha_20260821.json",
    }
    bridge = QmtRpcBridge(
        QmtRpcCfg(host="10.0.0.8", port=60001, timeout=9),
        token_path=token, client_factory=_FakeClient,
    )

    result = bridge.publish(payload)

    assert result == PublishResult(
        status="accepted", strategy="alpha", date="2026-08-21",
        checksum=payload["checksum"], filename="targets_alpha_20260821.json",
    )
    client = _FakeClient.instances[-1]
    assert client.connected == ("10.0.0.8", 60001, "secret-token", 9.0)
    assert client.calls == [("rpc.publish_targets", (payload,))]
    assert client.closed


def test_local_bridge_execution_status_and_atomic_fills_pull(tmp_path):
    fills = {"strategy": "alpha", "date": "2026-08-21", "fills": []}
    _FakeClient.responses["rpc.execution_status"] = {
        "found": True, "journal": {"status": "submitted"}, "fills": fills,
    }
    bridge = QmtRpcBridge(QmtRpcCfg(), token_path=tmp_path / "missing",
                          client_factory=_FakeClient)

    status = bridge.execution_status("alpha", "2026-08-21")
    path = bridge.pull_fills("alpha", "2026-08-21", tmp_path / "fills")

    assert status["journal"]["status"] == "submitted"
    assert path == tmp_path / "fills" / "fills_alpha_20260821.json"
    assert json.loads(path.read_text(encoding="utf-8")) == fills
    assert not path.with_suffix(".json.tmp").exists()


def test_local_bridge_pull_returns_none_when_server_has_no_fills(tmp_path):
    _FakeClient.responses["rpc.execution_status"] = {"found": False}
    bridge = QmtRpcBridge(QmtRpcCfg(), client_factory=_FakeClient)

    assert bridge.pull_fills("alpha", "2026-08-21", tmp_path) is None


def test_run_signal_remains_non_trading():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "run_signal.py").read_text(
        encoding="utf-8"
    )
    assert "publish_targets" not in source
    assert "QmtRpcBridge" not in source


def test_publish_cli_resolves_payload_profile_without_default_fallback(
        tmp_path, monkeypatch, capsys):
    from scripts import publish_targets as cli

    target = tmp_path / "target.json"
    payload = _payload(live_profile="remote_qmt")
    target.write_text(json.dumps(payload), encoding="utf-8")
    profiles = LiveProfilesCfg(
        default="local_qmt",
        profiles={
            "local_qmt": LiveProfileCfg(name="本地 QMT"),
            "remote_qmt": LiveProfileCfg(
                name="远端 QMT", qmt={"rpc": {"host": "10.0.0.8", "port": 60001}}
            ),
        },
    )
    seen = {}

    class FakeBridge:
        def __init__(self, rpc):
            seen["rpc"] = rpc

        def publish(self, sent):
            seen["payload"] = sent
            return PublishResult("accepted", "alpha", "2026-08-21",
                                 payload["checksum"], "targets_alpha_20260821.json")

    monkeypatch.setattr(cli, "load_live_profiles", lambda: profiles)
    monkeypatch.setattr(cli, "QmtRpcBridge", FakeBridge)

    assert cli.main([str(target)]) == 0
    assert seen["rpc"].host == "10.0.0.8"
    assert seen["payload"] == payload
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"


def test_publish_cli_rejects_missing_exact_profile(tmp_path, monkeypatch, capsys):
    from scripts import publish_targets as cli

    target = tmp_path / "target.json"
    target.write_text(json.dumps(_payload(live_profile="missing")), encoding="utf-8")
    profiles = LiveProfilesCfg(
        default="local_qmt",
        profiles={"local_qmt": LiveProfileCfg(name="本地 QMT")},
    )
    monkeypatch.setattr(cli, "load_live_profiles", lambda: profiles)
    monkeypatch.setattr(
        cli, "QmtRpcBridge",
        lambda *_args: pytest.fail("悬空引用不得回退默认配置或创建连接"),
    )

    assert cli.main([str(target)]) == 1
    assert "不存在的实盘配置" in capsys.readouterr().err
