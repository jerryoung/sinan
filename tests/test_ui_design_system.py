"""全平台 UI 设计系统契约：共享主题、流程导航与无表情图标。"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
THEME = ROOT / "ui" / "theme.py"
CONFIG = ROOT / ".streamlit" / "config.toml"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shared_theme_module_exposes_platform_primitives():
    source = _source(THEME)
    ast.parse(source)
    for name in ("apply_theme", "page_header", "workflow_bar", "lifecycle_status"):
        assert f"def {name}(" in source
    for token in ("--sn-canvas", "--sn-surface", "--sn-primary",
                  "--sn-success", "--sn-warning", "--sn-danger"):
        assert token in source
    assert 'font-family: "Material Symbols Rounded"' in source


def test_collapsed_sidebar_keeps_reopen_control_accessible():
    source = _source(THEME)
    assert '[data-testid="stHeader"]' in source
    assert "height: 0;" not in source
    assert '[data-testid="stToolbar"],' not in source
    assert '[data-testid="stExpandSidebarButton"]' in source


def test_app_applies_theme_and_uses_real_icon_library():
    source = _source(APP)
    assert "apply_theme()" in source
    assert source.index("apply_theme()") < source.index("st.navigation(")
    assert "st.logo(" in source
    for icon in ("monitoring", "science", "tune", "settings",
                 "candlestick_chart", "database", "sync"):
        assert f':material/{icon}:' in source
    for emoji in ("🧭", "📊", "🧪", "⚙️", "🛠️", "📈", "🗄️", "🔄"):
        assert emoji not in source


def test_streamlit_theme_matches_selected_visual_tokens():
    source = _source(CONFIG)
    assert 'base = "dark"' in source
    assert 'primaryColor = "#5B7CFA"' in source
    assert 'backgroundColor = "#0B1118"' in source
    assert 'secondaryBackgroundColor = "#111A25"' in source


def test_every_page_uses_shared_page_header():
    pages = (
        "shadow.py", "backtest.py", "strategy_config.py", "settings_page.py",
        "quotes.py", "warehouse.py", "updater.py",
    )
    for filename in pages:
        source = _source(ROOT / "ui" / filename)
        assert "page_header(" in source, filename


def test_core_flow_pages_show_the_three_stage_workflow():
    expected = {
        "backtest.py": 'workflow_bar("backtest")',
        "quotes.py": 'workflow_bar("data")',
        "warehouse.py": 'workflow_bar("data")',
        "updater.py": 'workflow_bar("data")',
    }
    for filename, call in expected.items():
        assert call in _source(ROOT / "ui" / filename), filename
    assert "lifecycle_status(" in _source(ROOT / "ui" / "shadow.py")


def test_strategy_dashboard_matches_option_one_lifecycle_workspace():
    source = _source(ROOT / "ui" / "shadow.py")
    assert "load_live_profiles" in source
    assert "resolve_live_profile" in source
    assert "lifecycle_status(" in source
    assert "_render_decision_rail" not in source
    for label in ("策略已更新", "回测已验证", "影子运行中", "持仓明细"):
        assert label in source
    assert "height=190" in source


def test_sidebar_uses_option_one_task_groups():
    source = _source(APP)
    for group in ('"数据"', '"回测与实盘"', '"策略与设置"'):
        assert group in source
    assert '"量化策略"' not in source
    assert '"数据中心"' not in source


def test_option_one_primary_action_uses_blue_token():
    source = _source(THEME)
    rule = source.split(".st-key-shadow_update_targets button[kind=\"primary\"]", 1)[1]
    rule = rule.split("}", 1)[0]
    assert "var(--sn-primary)" in rule
    assert "var(--sn-danger)" not in rule
    assert ".stButton button p" in source
    assert "color: inherit" in source.split(".stButton button p", 1)[1].split("}", 1)[0]


def test_legacy_backtest_report_gets_dark_theme_without_head_tag():
    from ui.backtest import _legacy_report_html

    original = "<meta charset='utf-8'><style>body{background:#fff}</style><h1>报告</h1>"
    themed = _legacy_report_html(original)
    assert themed != original
    assert "#111A25" in themed
    assert themed.index("#111A25") > themed.index("background:#fff")
