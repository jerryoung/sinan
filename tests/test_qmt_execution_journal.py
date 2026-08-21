"""QMT 委托执行日志：持久化基线、确定性备注与最多一次提交。"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from qmt_shell import sinan_qmt as rpc_server
from sinan.live.targets import targets_checksum


def _payload(targets=None, checksum=None):
    targets = targets or {"510300": 0.5}
    return {
        "strategy": "alpha",
        "date": "2026-08-21",
        "generated_at": "2026-08-21T14:35:00",
        "data_cutoff": "2026-08-20",
        "targets": targets,
        "checksum": checksum or targets_checksum(targets),
        "capital": 100_000,
        "qmt": {
            "algo": {
                "quote_mode": "limit", "price_offset": 0.002,
                "max_order_qty": 5_000,
            }
        },
    }


def _planned_execution():
    return {
        "strategy": "alpha", "date": "2026-08-21", "checksum": "abc",
        "status": "planned", "created_at": "2026-08-21T14:45:00",
        "updated_at": "2026-08-21T14:45:00",
        "baseline": {"cash": 100_000.0, "pos": {}},
        "prices": {"510300": 4.8},
        "orders": [{
            "sequence": 1, "remark": "alpha#20260821#1",
            "symbol": "510300", "qmt_code": "510300.SH", "side": "buy",
            "qty": 100, "reference_price": 4.8, "op": 23,
            "price_type": 11, "order_price": 4.81, "status": "planned",
        }],
    }


def test_prepare_execution_persists_baseline_and_deterministic_chunks(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        rpc_server, "load_ledger",
        lambda *_args: {"cash": 100_000.0, "pos": {"159941": 100}},
    )

    execution = rpc_server.prepare_execution(
        _payload(targets={"510300": 0.6}),
        {"510300": 4.8, "159941": 2.0},
        now=datetime(2026, 8, 21, 14, 45),
    )

    assert execution["status"] == "planned"
    assert execution["baseline"] == {
        "cash": 100_000.0, "pos": {"159941": 100}
    }
    assert [o["sequence"] for o in execution["orders"]] == [1, 2, 3, 4]
    assert [o["remark"] for o in execution["orders"]] == [
        rpc_server.make_remark("alpha", "20260821", seq)
        for seq in (1, 2, 3, 4)
    ]
    assert all(len(o["remark"]) < 24 for o in execution["orders"])
    assert execution["orders"][0]["side"] == "sell"
    path = tmp_path / "executions" / "execution_alpha_20260821.json"
    assert json.loads(path.read_text(encoding="utf-8")) == execution
    assert not path.with_suffix(".json.tmp").exists()


def test_prepare_execution_resumes_same_checksum_without_replanning(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        rpc_server, "load_ledger", lambda *_args: {"cash": 100_000.0, "pos": {}}
    )
    payload = _payload()
    first = rpc_server.prepare_execution(payload, {"510300": 5.0})
    second = rpc_server.prepare_execution(payload, {"510300": 10.0})

    assert second == first
    assert second["orders"][0]["reference_price"] == 5.0


def test_prepare_execution_rejects_checksum_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        rpc_server, "load_ledger", lambda *_args: {"cash": 100_000.0, "pos": {}}
    )
    rpc_server.prepare_execution(_payload(), {"510300": 5.0})

    with pytest.raises(ValueError, match="checksum 冲突"):
        rpc_server.prepare_execution(
            _payload(targets={"510300": 0.4}), {"510300": 5.0}
        )


def test_passorder_is_preceded_by_submitting_journal(monkeypatch):
    writes = []
    calls = []
    execution = _planned_execution()
    monkeypatch.setattr(
        rpc_server, "save_execution",
        lambda value: writes.append(copy.deepcopy(value)),
    )
    monkeypatch.setattr(rpc_server, "passorder", lambda *args: calls.append(args))
    monkeypatch.setattr(rpc_server, "_ACCOUNT", {"id": "8888", "type": "STOCK"})

    result = rpc_server.submit_execution(object(), execution)

    assert writes[0]["orders"][0]["status"] == "submitting"
    assert len(calls) == 1
    assert calls[0][9] == "alpha#20260821#1"
    assert writes[-1]["orders"][0]["status"] == "submitted"
    assert result["status"] == "submitted"


def test_passorder_exception_becomes_uncertain_and_is_never_retried(monkeypatch):
    calls = []
    execution = _planned_execution()
    monkeypatch.setattr(rpc_server, "save_execution", lambda _value: None)

    def crash(*args):
        calls.append(args)
        raise RuntimeError("unknown after side effect")

    monkeypatch.setattr(rpc_server, "passorder", crash)

    first = rpc_server.submit_execution(object(), execution)
    second = rpc_server.submit_execution(object(), first)

    assert len(calls) == 1
    assert first["status"] == second["status"] == "uncertain"
    assert second["orders"][0]["status"] == "uncertain"
    assert "RuntimeError" in second["orders"][0]["error"]


def test_existing_submitting_state_is_recovered_as_uncertain_without_call(
        monkeypatch):
    execution = _planned_execution()
    execution["status"] = "submitting"
    execution["orders"][0]["status"] = "submitting"
    called = []
    monkeypatch.setattr(rpc_server, "save_execution", lambda _value: None)
    monkeypatch.setattr(rpc_server, "passorder", lambda *args: called.append(args))

    result = rpc_server.submit_execution(object(), execution)

    assert called == []
    assert result["status"] == "uncertain"
    assert "重启" in result["orders"][0]["error"]


def test_publish_rejects_replacement_after_submission_started(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    original = _payload()
    rpc_server._publish_targets(original)
    execution = _planned_execution()
    execution["checksum"] = original["checksum"]
    execution["status"] = "submitting"
    rpc_server.save_execution(execution)

    with pytest.raises(ValueError, match="执行已开始"):
        rpc_server._publish_targets(_payload(targets={"510300": 0.4}))

    stored = json.loads(
        (tmp_path / "targets" / "targets_alpha_20260821.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["checksum"] == original["checksum"]


def test_publish_replacement_discards_unstarted_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        rpc_server, "load_ledger", lambda *_args: {"cash": 100_000.0, "pos": {}}
    )
    original = _payload()
    rpc_server._publish_targets(original)
    rpc_server.prepare_execution(original, {"510300": 5.0})

    result = rpc_server._publish_targets(_payload(targets={"510300": 0.4}))

    assert result["status"] == "replaced"
    assert rpc_server.load_execution("alpha", "2026-08-21") is None


def test_do_rebalance_only_prepares_and_submits_journal(monkeypatch):
    payload = _payload()
    prepared = _planned_execution()
    calls = []
    monkeypatch.setattr(rpc_server, "_load_today_targets", lambda _now: [payload])
    monkeypatch.setattr(rpc_server, "_snapshot", lambda: ({}, 0.0, 100_000.0))
    monkeypatch.setattr(rpc_server, "load_ledger", lambda *_args: prepared["baseline"])
    monkeypatch.setattr(
        rpc_server, "prepare_execution",
        lambda sent, prices, now=None: calls.append(("prepare", sent, prices)) or prepared,
    )
    monkeypatch.setattr(
        rpc_server, "submit_execution",
        lambda C, execution: calls.append(("submit", C, execution)) or execution,
    )
    monkeypatch.setattr(
        rpc_server, "save_ledger",
        lambda *_args: pytest.fail("报单阶段不得预先修改账本"),
    )
    monkeypatch.setattr(
        rpc_server, "_write_fills",
        lambda *_args: pytest.fail("报单阶段不得把计划委托冒充成交"),
    )

    C = type("C", (), {
        "get_full_tick": lambda self, codes: {
            code: {"lastPrice": 4.8} for code in codes
        }
    })()
    rpc_server.do_rebalance(C)

    assert [call[0] for call in calls] == ["prepare", "submit"]
