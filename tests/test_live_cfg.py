"""实盘设置(live)测试:全局 QMT 缺省 与 策略级整体覆盖(resolve_qmt)。

只 import sinan.config——qmt 配置对信号/回测无影响,仅决定 targets
透传薄壳的执行参数,解析规则在配置层一处收口。
"""
import pytest

from sinan.config import (LiveCfg, Settings, StrategyCfg, check_qmt,
                          load_settings, resolve_qmt)


def _strategy(**kw) -> StrategyCfg:
    base = dict(name="s", strategy="donchian", universe=["510300"])
    base.update(kw)
    return StrategyCfg(**base)


# ---------------------------------------------------------------- 模型缺省
def test_settings_live_defaults():
    """未配置 live 段的旧 settings.yaml 必须原义解析:engine=qmt、qmt 空。"""
    s = Settings()
    assert s.live.engine == "qmt"
    assert s.live.qmt == {}


def test_live_engine_rejects_unknown():
    """engine 是受控枚举:未接入的引擎名在配置层直接拒绝,不留到执行时。"""
    with pytest.raises(ValueError):
        LiveCfg(engine="ptrade")


def test_repo_settings_yaml_parses():
    """仓库自带 settings.yaml(含 live 段)能被 Settings 正常解析。"""
    s = load_settings()
    assert s.live.engine == "qmt"


# ---------------------------------------------------------------- resolve_qmt
def test_resolve_none_when_both_empty():
    """全局与策略皆未配置 → None,targets 不写 qmt 字段(薄壳用内置缺省)。"""
    assert resolve_qmt(Settings(), _strategy()) is None


def test_resolve_falls_back_to_global():
    """策略未配置 → 用全局 live.qmt(系统设置·实盘设置)。"""
    g = {"account": "10001", "algo": {"quote_mode": "limit", "price_offset": 0.003}}
    s = Settings(live=LiveCfg(qmt=g))
    assert resolve_qmt(s, _strategy()) == g


def test_resolve_strategy_overrides_whole():
    """策略配置了 → 整体覆盖:不与全局做键级合并,全局 account 不得渗入。"""
    s = Settings(live=LiveCfg(qmt={"account": "10001",
                                   "algo": {"price_offset": 0.01}}))
    cfg = _strategy(qmt={"algo": {"quote_mode": "limit"}})
    out = resolve_qmt(s, cfg)
    assert out == {"algo": {"quote_mode": "limit"}}
    assert "account" not in out


def test_resolve_returns_deep_copy():
    """返回深拷贝:嵌套 algo 也不得与配置对象共享引用。

    浅拷贝下 out["algo"]["price_offset"] = x 会回写 Settings,面板长驻进程
    里多策略批量出 targets 时污染会跨策略扩散。
    """
    s = Settings(live=LiveCfg(qmt={"account": "10001",
                                   "algo": {"price_offset": 0.002}}))
    out = resolve_qmt(s, _strategy())
    out["account"] = "tampered"
    out["algo"]["price_offset"] = 9.9
    assert s.live.qmt["account"] == "10001"
    assert s.live.qmt["algo"]["price_offset"] == pytest.approx(0.002)

    cfg = _strategy(qmt={"algo": {"price_offset": 0.003}})
    out2 = resolve_qmt(Settings(), cfg)
    out2["algo"]["price_offset"] = 9.9
    assert cfg.qmt["algo"]["price_offset"] == pytest.approx(0.003)


def test_resolve_empty_dict_equals_unconfigured():
    """策略 `qmt: {}` 与不写该键等价——都回退全局(docstring 明示的语义)。"""
    s = Settings(live=LiveCfg(qmt={"account": "888"}))
    assert resolve_qmt(s, _strategy(qmt={})) == {"account": "888"}


# ---------------------------------------------------------------- 坏值拦截
def test_check_qmt_rejects_bad_algo_types():
    """algo 约定键类型错误在出 targets 时就拒绝,不留到 14:45 炸薄壳。

    薄壳 do_rebalance 的 float(price_offset) 无逐策略兜底,一个坏值会让
    当日全部策略的调仓中断——爆炸半径远大于"这一个策略不下单"。
    """
    with pytest.raises(ValueError, match="price_offset"):
        check_qmt({"algo": {"price_offset": "0.2%"}}, source="x")
    with pytest.raises(ValueError, match="max_order_qty"):
        check_qmt({"algo": {"max_order_qty": "很多"}}, source="x")
    with pytest.raises(ValueError, match="quote_mode"):
        check_qmt({"algo": {"quote_mode": "lastest"}}, source="x")   # 拼写错误
    with pytest.raises(ValueError, match="必须是字典"):
        check_qmt({"algo": "latest"}, source="x")


def test_check_qmt_passes_through_extras():
    """额外键与缺省结构照单全收:"sinan 不解释 qmt 内容"仍然成立。"""
    check_qmt({}, source="x")
    check_qmt({"account": "888"}, source="x")                 # 无 algo
    check_qmt({"algo": {"未来键": {"任意": 1}, "quote_mode": "limit"}}, source="x")


def test_resolve_qmt_raises_on_bad_value():
    """坏值经全局或策略任一通道进入,resolve 阶段即抛,信息含来源。"""
    s = Settings(live=LiveCfg(qmt={"algo": {"price_offset": "abc"}}))
    with pytest.raises(ValueError, match="全局实盘设置"):
        resolve_qmt(s, _strategy())
    with pytest.raises(ValueError, match="策略 s"):
        resolve_qmt(Settings(), _strategy(qmt={"algo": {"max_order_qty": None}}))


# ---------------------------------------------------------------- YAML 留空
def test_yaml_null_sections_fall_back_to_defaults(tmp_path):
    """`live:` / `qmt:` 写成空行(YAML null)按未配置处理,不得让加载崩溃。

    settings.yaml 注释提到"空 = 不写 qmt 字段",用户按直觉删掉 {} 后
    若抛 ValidationError,run_signal/nightly/面板会全线不可用。
    """
    for text in ("live:\n", "live:\n  engine: qmt\n  qmt:\n"):
        p = tmp_path / "s.yaml"
        p.write_text(text, encoding="utf-8")
        s = load_settings(p)
        assert s.live.engine == "qmt" and s.live.qmt == {}
        assert resolve_qmt(s, _strategy()) is None


def test_yaml_live_section_roundtrip(tmp_path):
    """settings.yaml 的 live 段解析:account/algo 原样进 Settings。"""
    p = tmp_path / "settings.yaml"
    p.write_text("live:\n  engine: qmt\n  qmt:\n    account: '888'\n"
                 "    algo: {quote_mode: limit, max_order_qty: 5000}\n",
                 encoding="utf-8")
    s = load_settings(p)
    assert s.live.qmt["account"] == "888"
    assert s.live.qmt["algo"]["max_order_qty"] == 5000
