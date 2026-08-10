"""Task 7 执行层测试:targets 风控裁剪 / payload 校验和往返 / 各失败分支 / ManualBroker / notify。

只 import sinan.live.* 与已定契约(config),不触碰其他 Agent 并行编写的模块。
"""
import json
from datetime import datetime, timedelta

import pytest

from sinan.config import RiskCfg, Settings
from sinan.live.broker import ManualBroker, MiniQmtBroker
from sinan.live.notify import notify
from sinan.live.targets import (
    TargetsInvalid,
    apply_risk,
    build_payload,
    load_and_validate,
    save_targets,
)


def make_settings(**risk) -> Settings:
    return Settings(risk=RiskCfg(**risk))


# ---------------------------------------------------------------- apply_risk
def test_risk_per_symbol_cap():
    """单标的上限:超限标的裁到 cap,其余不动,并给出触发说明。"""
    s = make_settings(max_weight_per_symbol=0.34, max_total_weight=10.0,
                      max_daily_turnover=10.0)
    w, msgs = apply_risk({"A": 0.50, "B": 0.20}, current_weights={},
                         total_asset=1e6, adv20=None, settings=s)
    assert w["A"] == pytest.approx(0.34)
    assert w["B"] == pytest.approx(0.20)
    assert any("单标的上限" in m and "A" in m for m in msgs)


def test_risk_total_weight_proportional():
    """总仓位上限:超限时全体等比缩,比例关系保持不变。"""
    s = make_settings(max_weight_per_symbol=0.34, max_total_weight=0.6,
                      max_daily_turnover=10.0)
    w, msgs = apply_risk({"A": 0.50, "B": 0.50, "C": 0.20}, current_weights={},
                         total_asset=1e6, adv20=None, settings=s)
    # 先裁单标的:A/B → 0.34,C 0.20,Σ=0.88;再等比 ×(0.6/0.88)
    k = 0.6 / 0.88
    assert w["A"] == pytest.approx(0.34 * k)
    assert w["C"] == pytest.approx(0.20 * k)
    assert sum(w.values()) == pytest.approx(0.6)
    assert any("总仓位" in m for m in msgs)


def test_risk_daily_turnover_scales_delta():
    """单日调仓额:Σ|Δw| 超限按比例缩 Δ(向当前持仓收缩,含清仓腿)。"""
    s = make_settings(max_weight_per_symbol=1.0, max_total_weight=2.0,
                      max_daily_turnover=0.5)
    cur = {"A": 0.2, "C": 0.2}
    w, msgs = apply_risk({"A": 0.6, "B": 0.6}, current_weights=cur,
                         total_asset=1e6, adv20=None, settings=s)
    # 目标全集:A 0.6, B 0.6, C 0(持仓中未被提及 → 清仓)
    # Δ = (+0.4, +0.6, −0.2),Σ|Δ|=1.2 > 0.5 → k = 0.5/1.2
    k = 0.5 / 1.2
    assert w["A"] == pytest.approx(0.2 + 0.4 * k)
    assert w["B"] == pytest.approx(0.6 * k)
    assert w["C"] == pytest.approx(0.2 - 0.2 * k)
    turnover = sum(abs(w[x] - cur.get(x, 0.0)) for x in w)
    assert turnover == pytest.approx(0.5)
    assert any("调仓额" in m for m in msgs)


def test_risk_exit_symbol_included_as_zero():
    """持仓中而目标未提及的标的,输出必须显式给 0(否则薄壳不会卖出)。"""
    s = make_settings()
    w, _ = apply_risk({}, current_weights={"C": 0.2}, total_asset=1e6,
                      adv20=None, settings=s)
    assert w == {"C": 0.0}


def test_risk_liquidity_truncation():
    """流动性:|Δw|×total_asset ≤ adv20×pct,逐标的截断;无 adv20 的标的不受影响。"""
    s = make_settings(max_weight_per_symbol=1.0, max_total_weight=2.0,
                      max_daily_turnover=10.0, liquidity_pct_adv20=0.05)
    adv20 = {"A": 2_000_000.0}   # 限额 = 200 万 × 5% = 10 万 → |Δw| ≤ 0.1
    w, msgs = apply_risk({"A": 0.30, "B": 0.30}, current_weights={},
                         total_asset=1_000_000.0, adv20=adv20, settings=s)
    assert w["A"] == pytest.approx(0.10)
    assert w["B"] == pytest.approx(0.30)
    assert any("流动性" in m and "A" in m for m in msgs)


def test_risk_negative_weight_clamped():
    s = make_settings()
    w, msgs = apply_risk({"A": -0.1}, current_weights={}, total_asset=1e6,
                         adv20=None, settings=s)
    assert w["A"] == 0.0
    assert any("负权重" in m for m in msgs)


# ---------------------------------------------------------------- payload 往返
def test_payload_roundtrip(tmp_path):
    payload = build_payload({"510300": 0.25, "159915": 0.10},
                            strategy_name="donchian_etf",
                            date="2026-08-04", data_cutoff="2026-08-03",
                            params_fingerprint={"n_entry": 55, "atr_m": 3.0})
    for key in ("date", "generated_at", "data_cutoff", "strategy",
                "params_fingerprint", "targets", "checksum"):
        assert key in payload
    assert payload["date"] == "2026-08-04"          # date=执行日(可晚于 data_cutoff 一天)
    assert payload["data_cutoff"] == "2026-08-03"   # 数据截止另行留痕
    assert len(payload["params_fingerprint"]) == 12

    fp = save_targets(payload, tmp_path)
    assert fp.name == "targets_donchian_etf_20260804.json"

    out = load_and_validate(tmp_path, "2026-08-04", make_settings(), strategy="donchian_etf")
    assert out["targets"] == payload["targets"]
    assert out["checksum"] == payload["checksum"]


def test_params_fingerprint_deterministic():
    p1 = build_payload({}, strategy_name="s", date="2026-08-04", data_cutoff="2026-08-03",
                       params_fingerprint={"a": 1, "b": 2})
    p2 = build_payload({}, strategy_name="s", date="2026-08-04", data_cutoff="2026-08-03",
                       params_fingerprint={"b": 2, "a": 1})   # 键序无关
    p3 = build_payload({}, strategy_name="s", date="2026-08-04", data_cutoff="2026-08-03",
                       params_fingerprint={"a": 1, "b": 3})   # 参数改动必变
    assert p1["params_fingerprint"] == p2["params_fingerprint"]
    assert p1["params_fingerprint"] != p3["params_fingerprint"]
    # 传现成指纹串则原样使用
    p4 = build_payload({}, strategy_name="s", date="2026-08-04", data_cutoff="2026-08-03",
                       params_fingerprint="abcdef012345")
    assert p4["params_fingerprint"] == "abcdef012345"


# ---------------------------------------------------------------- 校验失败分支
def test_validate_missing_file(tmp_path):
    with pytest.raises(TargetsInvalid, match="不存在"):
        load_and_validate(tmp_path, "2026-08-04", make_settings(), strategy="s")


def test_validate_date_mismatch(tmp_path):
    payload = build_payload({"A": 0.1}, strategy_name="s",
                            date="2026-08-04", data_cutoff="2026-08-03", params_fingerprint={})
    save_targets(payload, tmp_path)
    # 文件名对不上目标日期 → 视同不存在
    with pytest.raises(TargetsInvalid):
        load_and_validate(tmp_path, "2026-08-05", make_settings(), strategy="s")
    # 文件名对但内容 date 被改 → 日期不符
    fp = tmp_path / "targets_s_20260804.json"
    bad = json.loads(fp.read_text(encoding="utf-8"))
    bad["date"] = "2026-08-03"
    fp.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TargetsInvalid, match="日期"):
        load_and_validate(tmp_path, "2026-08-04", make_settings(), strategy="s")


def test_validate_checksum_tampered(tmp_path):
    payload = build_payload({"A": 0.1}, strategy_name="s",
                            date="2026-08-04", data_cutoff="2026-08-03", params_fingerprint={})
    fp = save_targets(payload, tmp_path)
    bad = json.loads(fp.read_text(encoding="utf-8"))
    bad["targets"]["A"] = 0.99                       # 篡改目标仓位
    fp.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TargetsInvalid, match="校验"):
        load_and_validate(tmp_path, "2026-08-04", make_settings(), strategy="s")


def test_validate_stale_generated_at(tmp_path):
    payload = build_payload({"A": 0.1}, strategy_name="s",
                            date="2026-08-04", data_cutoff="2026-08-03", params_fingerprint={})
    stale = datetime.now() - timedelta(hours=9)
    payload["generated_at"] = stale.isoformat(timespec="seconds")
    save_targets(payload, tmp_path)
    with pytest.raises(TargetsInvalid, match="时效"):
        load_and_validate(tmp_path, "2026-08-04",
                          make_settings(targets_max_age_hours=8.0), strategy="s")
    # 阈值放宽则通过
    out = load_and_validate(tmp_path, "2026-08-04",
                          make_settings(targets_max_age_hours=24.0), strategy="s")
    assert out["targets"] == payload["targets"]


# ---------------------------------------------------------------- ManualBroker
def test_manual_broker_state_roundtrip(tmp_path):
    state = tmp_path / "manual_state.json"
    msgs = []
    b = ManualBroker(state, notify_fn=lambda m, **kw: msgs.append(m))
    b.set_cash(100_000.0)
    b.set_position("510300", 1000, price=3.85)

    # 另起实例读同一状态文件 → 账户状态完整往返
    b2 = ManualBroker(state, notify_fn=lambda m, **kw: None)
    assert b2.get_cash() == pytest.approx(100_000.0)
    pos = b2.get_positions()
    assert pos["510300"].qty == 1000
    assert pos["510300"].avail_qty == 1000           # 默认可用 = 持有
    assert pos["510300"].price == pytest.approx(3.85)

    # order_target 只记录并推送,不改变持仓
    res = b.order_target("510300", 2000)
    assert res.ok and res.symbol == "510300" and res.target_qty == 2000
    assert msgs and "510300" in msgs[-1]
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["pending"][0]["symbol"] == "510300"
    assert st["pending"][0]["target_qty"] == 2000
    assert b.get_positions()["510300"].qty == 1000   # 持仓未被自动改动


def test_miniqmt_broker_requires_xtquant():
    try:
        # 与 MiniQmtBroker 构造时相同的导入:xtquant 基础包可能装了,
        # 但交易模块 xttrader 依赖 QMT 客户端本体,mac/无 QMT 环境必失败
        from xtquant import xttrader, xttype  # noqa: F401
        pytest.skip("本机可用完整 xtquant,跳过缺依赖分支")
    except ImportError:
        pass
    with pytest.raises(RuntimeError, match="miniQMT"):
        MiniQmtBroker("8888888888")


# ---------------------------------------------------------------- notify
def test_notify_empty_webhook_no_http(monkeypatch):
    import urllib.request

    def boom(*a, **kw):
        raise AssertionError("空 webhook 不应发起 HTTP 请求")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    notify("测试消息", webhook="")                   # 只写本地日志,不抛异常


def test_notify_webhook_posts_markdown(monkeypatch):
    import urllib.request

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = req.data.decode("utf-8")
        sent["timeout"] = timeout
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    notify("**目标仓位摘要**", webhook="https://qyapi.weixin.qq.com/hook")
    assert sent["url"].startswith("https://qyapi.weixin.qq.com")
    body = json.loads(sent["body"])
    assert body["msgtype"] == "markdown"
    assert "目标仓位摘要" in body["markdown"]["content"]
    assert sent["timeout"] == 3


def test_notify_webhook_error_swallowed(monkeypatch):
    import urllib.request

    def broken(req, timeout=None):
        raise OSError("网络不可达")

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    notify("x", webhook="https://qyapi.weixin.qq.com/hook")   # 异常吞掉只记日志


# ------------------------------------------------ 持仓数上限(sinan.risk)
def test_limit_positions_noop_under_limit():
    from sinan.risk import limit_positions
    tgt = {"A": 0.1, "B": 0.2}
    out, msgs = limit_positions(tgt, {}, 2)
    assert out == tgt and msgs == []
    out, msgs = limit_positions(tgt, {}, 0)     # 0 = 不限
    assert out == tgt and msgs == []


def test_limit_positions_blocks_new_entrant():
    """已持仓优先:新信号权重再大也不挤出持仓,超额新入场被裁 0。"""
    from sinan.risk import limit_positions
    out, msgs = limit_positions({"A": 0.05, "C": 0.50}, {"A": 0.05}, 1)
    assert out["A"] == 0.05 and out["C"] == 0.0
    assert any("C" in m for m in msgs)


def test_limit_positions_trims_overheld():
    """持仓本身超限(如调低上限后):按权重保留前 max_n,其余裁 0。"""
    from sinan.risk import limit_positions
    cur = {"A": 0.1, "B": 0.1, "C": 0.1}
    out, _ = limit_positions({"A": 0.30, "B": 0.10, "C": 0.20}, cur, 2)
    assert out["A"] == 0.30 and out["C"] == 0.20 and out["B"] == 0.0


def test_apply_risk_max_positions_wired():
    """apply_risk 接线:max_positions 生效且在权重裁剪之前决定谁在场。"""
    s = make_settings(max_positions=2, max_weight_per_symbol=0.34,
                      max_total_weight=10.0, max_daily_turnover=10.0)
    w, msgs = apply_risk({"A": 0.3, "B": 0.2, "C": 0.1}, current_weights={},
                         total_asset=1e6, adv20=None, settings=s)
    assert w["A"] == 0.3 and w["B"] == 0.2 and w["C"] == 0.0
    assert any("持仓数上限" in m for m in msgs)


# ---------------------------------------------------------------- 参考委托单
def test_ref_orders_lot_rounding_and_sides():
    from sinan.live.targets import build_ref_orders
    orders = build_ref_orders(
        {"A": 0.10, "B": 0.0}, current_weights={"B": 0.05}, capital=1_000_000,
        prices={"A": 4.567, "B": 100.0}, lot_sizes={"A": 100, "B": 10})
    by = {o["symbol"]: o for o in orders}
    # A:买入 0.10×100万/4.567 = 21896.6 股 → 整百 21800
    assert by["A"]["side"] == "buy" and by["A"]["qty"] == 21800
    assert by["A"]["est_amount"] == round(21800 * 4.567, 2)
    # B:清仓卖出 0.05×100万/100 = 500 → 整 10 张
    assert by["B"]["side"] == "sell" and by["B"]["qty"] == 500


def test_ref_orders_band_and_zero_qty_skipped():
    from sinan.live.targets import build_ref_orders
    orders = build_ref_orders(
        {"A": 0.011, "C": 0.0001}, current_weights={}, capital=1_000_000,
        prices={"A": 5.0, "C": 5.0}, lot_sizes={"A": 100, "C": 100},
        band=0.02)
    assert orders == []                          # 全部小于带宽 → 无委托
    orders2 = build_ref_orders(
        {"A": 0.03}, current_weights={}, capital=10_000,   # 300元/5 = 60股 < 1手
        prices={"A": 5.0}, lot_sizes={"A": 100})
    assert orders2 == []                         # 不足一手 → 跳过


def test_ref_orders_missing_price_skipped():
    from sinan.live.targets import build_ref_orders
    orders = build_ref_orders({"A": 0.1, "B": 0.1}, current_weights={},
                              capital=1e6, prices={"B": 2.0}, lot_sizes={})
    assert [o["symbol"] for o in orders] == ["B"]
