"""数据源注册表与源链构建测试(不打网络、不连 QMT)。

覆盖:
- 内置适配器经惰性 import 注册(akshare/tushare/qmt);
- 重名注册、未知名字、模块未注册三条错误路径;
- build_sources 的单源降级与全源失败;
- qmt 源在无 rpc_server 环境下构造即抛 DataSourceError(降级前提)。
"""
import pytest

from sinan.data.sources import base
from sinan.data.sources.base import (DataSource, DataSourceError,
                                     build_sources, create_source,
                                     register_source, source_names)


def test_builtin_sources_registered():
    # create_source 触发适配器模块的惰性 import + 注册
    assert create_source("akshare").name == "akshare"
    assert create_source("tushare").name == "tushare"
    names = source_names()
    for n in ("akshare", "tushare"):
        assert n in names


def test_qmt_source_unavailable_off_trading_machine(monkeypatch):
    # 连接失败时构造必须抛 DataSourceError,由 build_sources 降级。
    # 不依赖“运行测试的机器一定没开 rpc_server”——开发机可能正连着 QMT。
    from qmt_shell import qmt_sdk

    def _offline():
        raise ConnectionError("offline probe")

    monkeypatch.setattr(qmt_sdk, "connect_from_settings", _offline)
    with pytest.raises(DataSourceError):
        create_source("qmt")
    srcs = build_sources(["qmt", "akshare"])   # qmt 降级,akshare 顶上
    assert [s.name for s in srcs] == ["akshare"]


def test_create_source_unknown_name():
    with pytest.raises(ValueError, match="未知数据源"):
        create_source("no_such_source_xyz")


def test_duplicate_registration_rejected():
    @register_source("dup_test")
    class _A(DataSource):
        def get_bars(self, symbols, sec_type, start, end): ...
        def get_instruments(self, sec_type): ...
        def get_cb_terms(self): ...
        def get_calendar(self, start, end): ...

    with pytest.raises(ValueError, match="重名"):
        register_source("dup_test")(_A)
    assert create_source("dup_test").name == "dup_test"   # 首次注册仍有效


def test_adapter_module_without_registration(tmp_path, monkeypatch):
    # 模块存在但没调 register_source → 明确报错,不静默
    import sinan.data.sources as pkg
    (tmp_path / "ghost_source.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(pkg, "__path__", [str(tmp_path)])
    base._SOURCES.pop("ghost", None)
    with pytest.raises(ValueError, match="未调用 register_source"):
        create_source("ghost")


def test_build_sources_all_unavailable():
    with pytest.raises(DataSourceError, match="全部不可用"):
        build_sources(["no_such_a", "no_such_b"])


def test_build_sources_reports_errors():
    msgs = []
    srcs = build_sources(["no_such_a", "akshare"], on_error=msgs.append)
    assert [s.name for s in srcs] == ["akshare"]
    assert len(msgs) == 1 and "no_such_a" in msgs[0]


def test_settings_data_sources_configurable(tmp_path):
    from sinan.config import load_settings
    (tmp_path / "s.yaml").write_text("data:\n  sources: [qmt, akshare]\n",
                                     encoding="utf-8")
    assert load_settings(tmp_path / "s.yaml").data.sources == ["qmt", "akshare"]
    # 未配置 data 段时回落默认链
    (tmp_path / "s2.yaml").write_text("capital: 1\n", encoding="utf-8")
    assert load_settings(tmp_path / "s2.yaml").data.sources == ["akshare", "tushare"]


def test_settings_data_sources_are_normalized(tmp_path):
    from sinan.config import load_settings
    (tmp_path / "s.yaml").write_text(
        "data:\n  sources: [' QMT ', AKSHARE]\n",
        encoding="utf-8",
    )
    assert load_settings(tmp_path / "s.yaml").data.sources == ["qmt", "akshare"]


@pytest.mark.parametrize(
    ("yaml_text", "error"),
    [
        ("data:\n  sources: []\n", "至少配置一个数据源"),
        ("data:\n  sources: [akshare, AKSHARE]\n", "数据源不能重复"),
        ("data:\n  sources: [akshare, ' ']\n", "数据源名称不能为空"),
    ],
)
def test_settings_data_sources_reject_invalid_chains(tmp_path, yaml_text, error):
    from pydantic import ValidationError
    from sinan.config import load_settings

    (tmp_path / "s.yaml").write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValidationError, match=error):
        load_settings(tmp_path / "s.yaml")
