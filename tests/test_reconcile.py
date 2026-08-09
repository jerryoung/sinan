"""Task 7:fills vs targets 对账 + QmtShellBroker fills 解析 + 脚本/薄壳语法自检。"""
import ast
import json
from pathlib import Path

import pytest

from trend.live.broker import QmtShellBroker
from trend.live.reconcile import ReconcileReport, reconcile
from trend.live.targets import build_payload

ROOT = Path(__file__).resolve().parents[1]


def _payload(targets: dict) -> dict:
    return build_payload(targets, strategy_name="t", date="2026-08-04",
                         data_cutoff="2026-08-03", params_fingerprint={})


# ---------------------------------------------------------------- reconcile
def test_reconcile_ok_within_tolerance():
    rep = reconcile(_payload({"A": 0.30, "B": 0.20}),
                    {"A": 0.305, "B": 0.195}, tolerance=0.01)
    assert isinstance(rep, ReconcileReport)
    assert rep.ok and rep.deviations == []


def test_reconcile_deviation_triggers_notify():
    called = []
    rep = reconcile(_payload({"A": 0.30, "B": 0.20}), {"A": 0.25},
                    tolerance=0.01, notify_fn=lambda m: called.append(m))
    assert not rep.ok
    devs = {d["symbol"]: d for d in rep.deviations}
    assert set(devs) == {"A", "B"}                   # B 完全没成交 → 偏差 0.20
    assert devs["A"]["target"] == pytest.approx(0.30)
    assert devs["A"]["actual"] == pytest.approx(0.25)
    assert devs["B"]["actual"] == pytest.approx(0.0)
    assert called                                     # 超阈值必须告警


def test_reconcile_extra_fill_symbol_exposed():
    """成交侧多出目标外的持仓(残留/误操作)同样是账实不符。"""
    rep = reconcile(_payload({"A": 0.30}), {"A": 0.30, "C": 0.10}, tolerance=0.01)
    assert not rep.ok
    assert any(d["symbol"] == "C" for d in rep.deviations)


def test_reconcile_no_notify_when_ok():
    called = []
    rep = reconcile(_payload({"A": 0.30}), {"A": 0.30},
                    tolerance=0.01, notify_fn=lambda m: called.append(m))
    assert rep.ok and not called


# ---------------------------------------------------------------- QmtShellBroker
def test_qmt_shell_read_fills_roundtrip(tmp_path):
    fills_payload = {
        "date": "2026-08-04", "total_asset": 1_000_000.0, "cash": 400_000.0,
        "weights": {"510300": 0.6},
        "fills": [{"symbol": "510300", "side": "buy", "qty": 1000, "price": 3.85}],
        "positions": {"510300": {"qty": 155000, "avail_qty": 154000, "price": 3.87}},
    }
    (tmp_path / "fills_20260804.json").write_text(
        json.dumps(fills_payload, ensure_ascii=False), encoding="utf-8")

    out = QmtShellBroker.read_fills(tmp_path, "2026-08-04")
    assert out["weights"] == {"510300": 0.6}
    assert out["fills"][0]["qty"] == 1000

    b = QmtShellBroker(tmp_path / "targets", tmp_path)
    assert b.get_cash() == pytest.approx(400_000.0)
    pos = b.get_positions()
    assert pos["510300"].qty == 155000 and pos["510300"].avail_qty == 154000


def test_qmt_shell_read_fills_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        QmtShellBroker.read_fills(tmp_path, "2026-08-04")


def test_qmt_shell_order_target_no_direct_order(tmp_path):
    """order_target 不直接下单:只聚合目标,真正执行走 targets 文件桥接。"""
    b = QmtShellBroker(tmp_path / "targets", tmp_path / "fills")
    res = b.order_target("510300", 1000)
    assert res.ok
    assert "targets" in res.msg or "桥接" in res.msg
    assert b.pending == {"510300": 1000}


# ---------------------------------------------------------------- 脚本语法自检
# 三个调度脚本 + QMT 薄壳都是薄 CLI/模板,不在测试中 import(依赖其他 Agent
# 并行编写的模块 / QMT 注入的运行环境),ast.parse 保证语法与缩进正确。
@pytest.mark.parametrize("rel", [
    "scripts/daily_update.py",
    "scripts/run_signal.py",
    "scripts/run_backtest.py",
    "qmt_shell/shell_strategy.py",
])
def test_scripts_syntax(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    ast.parse(src, filename=rel)
