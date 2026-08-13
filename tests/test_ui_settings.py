"""设置页与 pydantic 模型的一致性:表单缺省不得另写一份字面量。

背景:表单曾把 max_positions 的缺省写成 12,而模型默认是 0(不限)。
settings.yaml 缺该键时面板显示 12,用户改别的项点保存就把"不限"静默
变成 12——风控行为被一次无关操作改掉。此测试用静态检查锁住这条约定:
表单取缺省一律经 _DEF/_DEF_EXEC/_DEF_RISK,不许出现裸字面量。
"""
import ast
import re
from pathlib import Path

from sinan.config import ExecutionCfg, RiskCfg, Settings

PAGE = Path(__file__).resolve().parents[1] / "ui" / "settings_page.py"
LIVE_PAGE = Path(__file__).resolve().parents[1] / "ui" / "live_profiles.py"
COMMON_PAGE = Path(__file__).resolve().parents[1] / "ui" / "common.py"
SRC = PAGE.read_text(encoding="utf-8")

#: 这些键的表单缺省必须来自模型。QMT RPC 已归入命名实盘配置。
MODEL_BACKED = {
    **RiskCfg().model_dump(),
    **ExecutionCfg().model_dump(),
    "capital": Settings().capital,
    "wecom_webhook": Settings().wecom_webhook,
}


def test_page_parses():
    ast.parse(SRC)


def test_settings_page_has_system_and_live_profile_tabs():
    assert 'st.tabs(["系统设置", "实盘配置"])' in SRC
    assert "render_live_profiles_page" in SRC
    assert LIVE_PAGE.exists()
    ast.parse(LIVE_PAGE.read_text(encoding="utf-8"))


def test_settings_page_no_longer_edits_inline_qmt():
    assert "配置全局 QMT 执行参数" not in SRC
    assert 'out["live"]' not in SRC
    assert "qmt_rpc" not in SRC


def test_live_profile_page_edits_qmt_rpc_connection():
    source = LIVE_PAGE.read_text(encoding="utf-8")
    assert "QmtRpcCfg" in source
    for label in ("QMT 数据连接", "连接地址", "端口", "超时"):
        assert label in source


def test_live_profile_page_can_verify_qmt_rpc_readiness():
    source = LIVE_PAGE.read_text(encoding="utf-8")
    assert "verify_qmt_rpc" in source
    assert '"验证 RPC"' in source
    for field in ("运行模式", "交易权限", "实时行情"):
        assert field in source


def test_settings_form_exposes_multi_source_priority():
    assert "st.multiselect" in SRC
    assert 'data.sources' in SRC
    for source in ("sina", "akshare", "tushare", "qmt"):
        assert f'"{source}"' in SRC


def test_strategy_form_uses_profile_reference_not_inline_qmt():
    source = COMMON_PAGE.read_text(encoding="utf-8")
    assert "live_profile" in source
    assert "load_live_profiles" in source
    assert "QMT 实盘执行(策略级覆盖" not in source
    assert 'out["qmt"]' not in source


def test_no_literal_defaults_for_model_backed_fields():
    """`get("字段", <字面量>)` 形式一律不允许——那就是第二份默认值。"""
    offenders = []
    for key in MODEL_BACKED:
        for m in re.finditer(rf'get\(\s*"{re.escape(key)}"\s*,\s*([^)]+)\)', SRC):
            default = m.group(1).strip()
            if not default.startswith("_DEF"):
                offenders.append(f"{key} -> {default}")
    assert not offenders, (
        "表单缺省必须取自 pydantic 模型(_DEF/_DEF_EXEC/_DEF_RISK),"
        f"发现字面量:{offenders}")


def test_model_backed_keys_are_actually_referenced():
    """防止本测试因字段改名而空转:每个键都应真的出现在表单里。"""
    missing = [k for k in MODEL_BACKED if f'"{k}"' not in SRC]
    assert not missing, f"表单未覆盖这些配置项(或已改名):{missing}"


def test_max_positions_default_matches_model():
    """回归锁:曾漂移的那一项,表单缺省必须等于模型的 0(不限)。"""
    assert RiskCfg().max_positions == 0
    m = re.search(r'get\(\s*"max_positions"\s*\)\s*,\s*(_DEF_RISK\["max_positions"\])',
                  SRC)
    assert m, "max_positions 的缺省未取自 _DEF_RISK"
