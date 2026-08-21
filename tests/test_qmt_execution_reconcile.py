"""QMT 委托/成交归因：只有真实 deal 能改变账本和 fills。"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from qmt_shell import sinan_qmt as rpc_server


def _execution(status="submitted", baseline=None, orders=None):
    return {
        "strategy": "alpha", "date": "2026-08-21", "checksum": "abc",
        "status": status, "created_at": "2026-08-21T14:45:00",
        "updated_at": "2026-08-21T14:45:00",
        "baseline": baseline or {"cash": 100_000.0, "pos": {}},
        "prices": {"510300": 4.8}, "targets": {"510300": 0.5},
        "orders": orders or [{
            "sequence": 1, "remark": "alpha#20260821#1",
            "symbol": "510300", "qmt_code": "510300.SH", "side": "buy",
            "qty": 100, "reference_price": 4.8, "op": 23,
            "price_type": 11, "order_price": 4.81, "status": status,
        }],
    }


def _order(status=50, traded=0, **extra):
    value = {
        "remark": "alpha#20260821#1", "symbol": "510300",
        "order_sys_id": "O1", "status_raw": status, "qty": 100,
        "traded_qty": traded, "cancel_qty": 0, "price": 4.81,
    }
    value.update(extra)
    return value


def _deal(trade_id="D1", qty=100, price=4.8, **extra):
    value = {
        "trade_id": trade_id, "order_sys_id": "O1",
        "remark": "alpha#20260821#1", "symbol": "510300",
        "side": "buy", "qty": qty, "price": price,
        "trade_time": "2026-08-21 14:46:00",
    }
    value.update(extra)
    return value


def test_submitted_order_without_deal_does_not_change_ledger():
    baseline = {"cash": 100_000.0, "pos": {}}

    result = rpc_server._reconcile_execution(
        _execution(baseline=baseline), qmt_orders=[_order()], qmt_deals=[]
    )

    assert result["ledger"] == baseline
    assert result["fills"] == []
    assert result["execution"]["status"] == "accepted"


def test_partial_and_full_deals_derive_state_and_ledger():
    partial = rpc_server._reconcile_execution(
        _execution(), [_order(status=55, traded=40)], [_deal(qty=40)]
    )
    assert partial["execution"]["status"] == "partially_filled"
    assert partial["ledger"] == {"cash": 99_808.0, "pos": {"510300": 40}}

    full = rpc_server._reconcile_execution(
        _execution(), [_order(status=56, traded=100)], [_deal()]
    )
    assert full["execution"]["status"] == "filled"
    assert full["ledger"] == {"cash": 99_520.0, "pos": {"510300": 100}}
    assert full["fills"] == [_deal()]


@pytest.mark.parametrize("raw,expected", [(54, "canceled"), (57, "rejected")])
def test_terminal_order_without_deal_preserves_unchanged_ledger(raw, expected):
    result = rpc_server._reconcile_execution(
        _execution(), [_order(status=raw)], []
    )
    assert result["execution"]["status"] == expected
    assert result["ledger"] == {"cash": 100_000.0, "pos": {}}
    assert result["fills"] == []


def test_duplicate_deal_is_applied_once():
    deal = _deal()
    ledger, unique = rpc_server._rebuild_ledger(
        {"cash": 100_000.0, "pos": {}}, [deal, copy.deepcopy(deal)]
    )
    assert ledger == {"cash": 99_520.0, "pos": {"510300": 100}}
    assert unique == [deal]


class _OrderObject:
    m_strRemark = "alpha#20260821#1"
    m_strInstrumentID = "510300"
    m_strOrderSysID = "O1"
    m_nOrderStatus = 50
    m_nVolumeTotalOriginal = 100
    m_nVolumeTraded = 0
    m_dCancelAmount = 0
    m_dLimitPrice = 4.81


class _DealObject:
    m_strRemark = "alpha#20260821#1"
    m_strInstrumentID = "510300"
    m_strOrderSysID = "O1"
    m_strTradeID = "D1"
    m_nOffsetFlag = 48
    m_nVolume = 100
    m_dPrice = 4.8
    m_strTradeTime = "2026-08-21 14:46:00"


def test_collect_orders_and_deals_normalize_required_safe_fields(monkeypatch):
    def query(_account, _account_type, kind):
        return [_OrderObject()] if kind == "order" else [_DealObject()]

    monkeypatch.setattr(rpc_server, "get_trade_detail_data", query)
    monkeypatch.setattr(
        rpc_server, "timetag_to_datetime", lambda *_args: "2026-08-21 14:46:00",
        raising=False,
    )

    orders = rpc_server._collect_orders("20260821")
    deals = rpc_server._collect_deals("20260821")

    assert orders["alpha"][0] == _order()
    assert deals["alpha"][0] == _deal()


def test_compact_remark_is_attributed_through_execution_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    compact = rpc_server.make_remark(
        "combo_turtle_xsmom_x2", "20260821", 1
    )
    execution = _execution()
    execution["strategy"] = "combo_turtle_xsmom_x2"
    execution["orders"][0]["remark"] = compact
    rpc_server.save_execution(execution)
    order = _OrderObject()
    order.m_strRemark = compact
    monkeypatch.setattr(
        rpc_server, "get_trade_detail_data", lambda *_args: [order]
    )

    rows = rpc_server._collect_orders("20260821")

    assert list(rows) == ["combo_turtle_xsmom_x2"]
    assert rows["combo_turtle_xsmom_x2"][0]["remark"] == compact


def test_unreadable_required_field_is_error_not_empty_success(monkeypatch):
    class Broken(_OrderObject):
        @property
        def m_strInstrumentID(self):
            raise TypeError("cannot convert")

    monkeypatch.setattr(
        rpc_server, "get_trade_detail_data",
        lambda *_args: [Broken()],
    )

    rows = rpc_server._collect_orders("20260821")
    assert "m_strInstrumentID" in rows["alpha"][0]["_error"]
    result = rpc_server._reconcile_execution(
        _execution(), rows["alpha"], []
    )
    assert result["execution"]["status"] == "unreadable"


def test_refresh_writes_zero_deal_fills_without_mutating_baseline(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(rpc_server, "_trade_mode", lambda _C: "sim")
    execution = _execution()
    rpc_server.save_execution(execution)

    refreshed = rpc_server._refresh_execution(
        object(), execution, [_order()], [], prices={"510300": 4.8},
        now=datetime(2026, 8, 21, 15, 5),
    )

    ledger = json.loads(
        (tmp_path / "state" / "ledger_alpha.json").read_text(encoding="utf-8")
    )
    fills = json.loads(
        (tmp_path / "fills" / "fills_alpha_20260821.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger == execution["baseline"]
    assert fills["fills"] == []
    assert fills["orders"][0]["status"] == "accepted"
    assert fills["execution_status"] == "accepted"
    assert refreshed["status"] == "accepted"


def test_execution_status_rpc_reads_exact_journal_and_fills(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    execution = _execution()
    rpc_server.save_execution(execution)
    fills_dir = tmp_path / "fills"
    fills_dir.mkdir()
    fills = {"strategy": "alpha", "date": "2026-08-21", "fills": []}
    (fills_dir / "fills_alpha_20260821.json").write_text(
        json.dumps(fills), encoding="utf-8"
    )

    result = rpc_server.dispatch(
        {}, object(), "rpc.execution_status",
        ["alpha", "2026-08-21"], {}, False,
    )

    assert result == {"found": True, "journal": execution, "fills": fills}
    assert "execution_status" in rpc_server.dispatch(
        {}, object(), "rpc.health", [], {}, False
    )["capabilities"]


def test_snapshot_isolates_one_strategy_refresh_failure(monkeypatch, capsys):
    executions = [_execution(), dict(_execution(), strategy="beta")]
    seen = []
    monkeypatch.setattr(rpc_server, "_load_day_executions", lambda _day: executions)
    monkeypatch.setattr(rpc_server, "_collect_orders", lambda _ymd: {})
    monkeypatch.setattr(rpc_server, "_collect_deals", lambda _ymd: {})
    monkeypatch.setattr(rpc_server, "_snapshot", lambda: ({}, 0.0, 0.0))

    def refresh(_C, execution, *_args, **_kwargs):
        if execution["strategy"] == "alpha":
            raise RuntimeError("broken alpha")
        seen.append(execution["strategy"])

    monkeypatch.setattr(rpc_server, "_refresh_execution", refresh)

    rpc_server.do_snapshot(object())

    assert seen == ["beta"]
    assert "alpha" in capsys.readouterr().out


def test_load_day_executions_skips_one_corrupt_journal(tmp_path, monkeypatch,
                                                       capsys):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    valid = _execution()
    rpc_server.save_execution(valid)
    directory = tmp_path / "executions"
    (directory / "execution_bad_20260821.json").write_text(
        "{broken", encoding="utf-8"
    )

    loaded = rpc_server._load_day_executions("2026-08-21")

    assert [item["strategy"] for item in loaded] == ["alpha"]
    assert "execution_bad" in capsys.readouterr().out
