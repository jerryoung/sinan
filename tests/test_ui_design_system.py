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
    for name in ("apply_theme", "page_header", "workflow_bar", "status_kv"):
        assert f"def {name}(" in source
    for token in ("--sn-canvas", "--sn-surface", "--sn-primary",
                  "--sn-success", "--sn-warning", "--sn-danger"):
        assert token in source


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
        "shadow.py": 'workflow_bar("live")',
        "backtest.py": 'workflow_bar("backtest")',
        "quotes.py": 'workflow_bar("data")',
        "warehouse.py": 'workflow_bar("data")',
        "updater.py": 'workflow_bar("data")',
    }
    for filename, call in expected.items():
        assert call in _source(ROOT / "ui" / filename), filename


def test_strategy_dashboard_has_real_decision_rail_context():
    source = _source(ROOT / "ui" / "shadow.py")
    assert "load_live_profiles" in source
    assert "resolve_live_profile" in source
    for label in ("运行状态", "数据概况", "实盘配置", "风险概览"):
        assert label in source


def test_legacy_backtest_report_gets_dark_theme_without_head_tag():
    from ui.backtest import _legacy_report_html

    original = "<meta charset='utf-8'><style>body{background:#fff}</style><h1>报告</h1>"
    themed = _legacy_report_html(original)
    assert themed != original
    assert "#111A25" in themed
    assert themed.index("#111A25") > themed.index("background:#fff")
