"""对账接线测试:reconcile_fills 按 fills 自报的执行日找 targets 对账。

此前 reconcile.py 在生产代码里零引用——docstring 承诺的安全网是断开的。
接线点选在次日出信号时(run_signal):今天该不该照常下单,取决于上一次
执行到底成没成。这里锁住三件事:能发现真正的账实不符、不对正常情况
(影子模式/无对应 targets)制造噪声、结论以可留痕的形态返回。
"""
import json

import pytest

from sinan.live.reconcile import reconcile_fills
from sinan.live.targets import build_payload, save_targets, targets_path


@pytest.fixture
def tdir(tmp_path):
    d = tmp_path / "targets"
    d.mkdir()
    return d


def _write_targets(tdir, targets, *, strategy="s", date="2026-08-06"):
    return save_targets(
        build_payload(targets, strategy_name=strategy, date=date,
                      data_cutoff="2026-08-05", params_fingerprint={}), tdir)


def _fills(weights, *, date="2026-08-06"):
    return {"date": date, "strategy": "s", "weights": weights,
            "total_asset": 1e6}


# ---------------------------------------------------------------- 发现问题
def test_detects_shell_never_ran(tdir):
    """最要命的场景:targets 写出去了,薄壳压根没跑 —— 实际权重全空。"""
    _write_targets(tdir, {"A": 0.30, "B": 0.20})
    msgs = []
    rep = reconcile_fills(_fills({}), tdir, strategy="s", tolerance=0.02,
                          notify_fn=lambda m, **kw: msgs.append(m))
    assert not rep.ok
    assert {d["symbol"] for d in rep.deviations} == {"A", "B"}
    assert rep.date == "2026-08-06"
    assert msgs and "对账告警" in msgs[0]


def test_detects_partial_fill_beyond_tolerance(tdir):
    """部分成交超出容忍度要报;容忍度内的漂移不报。"""
    _write_targets(tdir, {"A": 0.30})
    bad = reconcile_fills(_fills({"A": 0.10}), tdir, strategy="s", tolerance=0.02)
    assert not bad.ok and bad.deviations[0]["diff"] == pytest.approx(-0.20)

    ok = reconcile_fills(_fills({"A": 0.29}), tdir, strategy="s", tolerance=0.02)
    assert ok.ok and ok.deviations == []


def test_detects_residual_position_not_in_targets(tdir):
    """目标里没有、账户里却有 —— 残留持仓/误操作同样是账实不符。"""
    _write_targets(tdir, {"A": 0.30})
    rep = reconcile_fills(_fills({"A": 0.30, "Z": 0.15}), tdir, strategy="s",
                          tolerance=0.02)
    assert not rep.ok and [d["symbol"] for d in rep.deviations] == ["Z"]


# ---------------------------------------------------------------- 不制造噪声
@pytest.mark.parametrize("fills,reason", [
    (None, "影子模式"),
    ({}, "影子模式"),
    ({"weights": {"A": 0.3}}, "未标注执行日"),
])
def test_skips_quietly_without_usable_fills(tdir, fills, reason):
    msgs = []
    rep = reconcile_fills(fills, tdir, strategy="s", tolerance=0.02,
                          notify_fn=lambda m, **kw: msgs.append(m))
    assert rep.ok and reason in rep.skipped
    assert msgs == [], "正常情况不该告警——噪声会让真正的告警被忽略"


def test_skips_when_no_targets_for_that_day(tdir):
    """薄壳跑了但那天我们没生成 targets:不是账实不符,记原因跳过。"""
    msgs = []
    rep = reconcile_fills(_fills({"A": 0.3}), tdir, strategy="s", tolerance=0.02,
                          notify_fn=lambda m, **kw: msgs.append(m))
    assert rep.ok and "无对应 targets" in rep.skipped
    assert rep.date == "2026-08-06"
    assert msgs == []


def test_skips_on_corrupt_targets_file(tdir):
    fp = targets_path(tdir, "s", "2026-08-06")
    fp.write_text("{ 坏的 JSON", encoding="utf-8")
    rep = reconcile_fills(_fills({"A": 0.3}), tdir, strategy="s", tolerance=0.02)
    assert rep.ok and "无法解析" in rep.skipped


def test_matches_strategy_and_date_exactly(tdir):
    """按 fills 自报的执行日与策略名取 targets,不会错配到别的日子/策略。"""
    _write_targets(tdir, {"A": 0.30}, strategy="s", date="2026-08-05")
    rep = reconcile_fills(_fills({"A": 0.30}, date="2026-08-06"), tdir,
                          strategy="s", tolerance=0.02)
    assert "无对应 targets" in rep.skipped          # 只认同一天
    rep2 = reconcile_fills(_fills({"A": 0.30}), tdir, strategy="其他策略",
                           tolerance=0.02)
    assert "无对应 targets" in rep2.skipped         # 只认同一策略


# ---------------------------------------------------------------- 留痕形态
def test_report_serializes_for_payload(tdir):
    _write_targets(tdir, {"A": 0.30})
    rep = reconcile_fills(_fills({"A": 0.05}), tdir, strategy="s", tolerance=0.02)
    d = rep.as_dict()
    assert set(d) == {"date", "ok", "skipped", "deviations"}
    assert d["ok"] is False and d["date"] == "2026-08-06"
    json.dumps(d)                       # 必须可直接进 targets payload


# ---------------------------------------------------------------- 接线自检
def test_run_signal_wires_reconcile():
    """run_signal 必须真的调用它——否则安全网又断了(此前正是如此)。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "run_signal.py"
           ).read_text(encoding="utf-8")
    assert "reconcile_fills(" in src
    assert 'payload["reconcile"]' in src, "对账结论必须随当日决策留痕"
    assert "settings.risk.reconcile_tolerance" in src, "容忍度须来自配置"
