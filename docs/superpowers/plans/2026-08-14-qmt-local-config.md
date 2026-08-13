# QMT Local Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 QMT 脚本从 `C:\sinan\config\qmt.json` 一次性读取全部机器配置，替换脚本时不再手工修改 Token 和白名单。

**Architecture:** 在现有 `qmt_shell/sinan_qmt.py` 内建立单一配置加载接口，集中处理默认值、JSON 解析、类型与安全校验；`init` 只消费加载后的配置。RPC 配置错误采用局部失败：记录安全日志并不启动 RPC，但保留 targets、快照和实盘推送。

**Tech Stack:** Python 3.6 兼容标准库（`json`、`ipaddress`、`os`）、pytest、QMT ContextInfo 回调。

## Global Constraints

- 服务器配置固定为 `C:\sinan\config\qmt.json`。
- `share_dir` 缺省为 `C:\sinan\var\runtime`。
- Token、白名单、RPC 与实盘推送配置统一保存在一个 JSON 文件。
- 配置仅在策略启动时读取，修改后重启策略生效，不实现热加载。
- 配置文件不存在时自动创建父目录和安全默认文件；默认 `rpc.enable=false`。
- 远程监听要求 Token 至少 32 位且白名单非空。
- 日志不得输出 Token 内容。
- 配置错误不得中止 targets、快照和实盘推送。
- QMT 脚本保持 Python 3.6 兼容，不引入第三方配置依赖。

---

### Task 1: 本地配置加载与校验

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:40-75`
- Test: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: JSON 文件路径；代码内安全默认值。
- Produces: `load_local_config(path=QMT_CONFIG_PATH) -> dict`，返回包含 `share_dir`、`rpc`、`live_push` 的完整配置；错误通过 `QmtConfigError` 表达。

- [ ] **Step 1: 写配置加载失败测试**

```python
def test_load_local_config_creates_safe_default_when_file_missing(tmp_path):
    path = tmp_path / "config" / "qmt.json"
    cfg = rpc_server.load_local_config(str(path))
    assert path.exists()
    assert cfg["share_dir"] == r"C:\sinan\var\runtime"
    assert cfg["rpc"]["enable"] is False
    assert cfg["rpc"]["token"] == ""
    assert cfg["rpc"]["allow_ips"] == []
    assert json.loads(path.read_text(encoding="utf-8")) == cfg


def test_load_local_config_reads_single_file(tmp_path):
    path = tmp_path / "qmt.json"
    path.write_text(json.dumps({
        "share_dir": r"D:\sinan\runtime",
        "rpc": {"enable": True, "host": "0.0.0.0", "port": 58620,
                "token": "x" * 32, "allow_trade": True,
                "allow_ips": ["120.245.101.210"]},
        "live_push": {"enable": True, "period": "5nSecond"},
    }), encoding="utf-8")
    cfg = rpc_server.load_local_config(str(path))
    assert cfg["share_dir"] == r"D:\sinan\runtime"
    assert cfg["rpc"]["token"] == "x" * 32
    assert cfg["rpc"]["allow_ips"] == ["120.245.101.210"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_qmt_bridge.py -k "load_local_config" -q`

Expected: FAIL，提示 `load_local_config` 不存在。

- [ ] **Step 3: 实现深配置模块接口**

在 `sinan_qmt.py` 中定义：

```python
QMT_CONFIG_PATH = r"C:\sinan\config\qmt.json"


class QmtConfigError(ValueError):
    pass


def load_local_config(path=QMT_CONFIG_PATH):
    """读取并校验服务器本地配置；文件缺失时创建安全缺省文件。"""
```

实现要求：返回新建的嵌套字典，不共享可变默认值；缺文件时递归创建父目录，以
UTF-8 和缩进 JSON 写入安全默认配置，其中 RPC 关闭；读取使用 UTF-8；Token 调用
`strip()`；校验布尔值、非空字符串、1..65535 端口、正数周期文本，以及每个单 IP
或 CIDR；远程 RPC 开启时校验 32 位 Token 和非空白名单。配置文件存在但内容错误时
抛出不含 Token 内容的 `QmtConfigError`。

- [ ] **Step 4: 补齐非法输入参数化测试**

```python
@pytest.mark.parametrize("patch,match", [
    ({"rpc": {"port": 70000}}, "port"),
    ({"rpc": {"allow_ips": ["错误IP"]}}, "allow_ips"),
    ({"rpc": {"enable": True, "host": "0.0.0.0",
              "token": "short", "allow_ips": ["1.2.3.4"]}}, "至少32位"),
])
def test_load_local_config_rejects_invalid_values(tmp_path, patch, match):
    path = tmp_path / "qmt.json"
    path.write_text(json.dumps(patch), encoding="utf-8")
    with pytest.raises(rpc_server.QmtConfigError, match=match):
        rpc_server.load_local_config(str(path))
```

- [ ] **Step 5: 运行配置测试**

Run: `python3 -m pytest tests/test_qmt_bridge.py -k "local_config" -q`

Expected: PASS。

- [ ] **Step 6: 提交配置加载模块**

```bash
git add qmt_shell/sinan_qmt.py tests/test_qmt_bridge.py
git commit -m "feat: load qmt machine config from json"
```

### Task 2: 启动接线与 RPC 局部失败

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:511-580`
- Test: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: `load_local_config(path) -> dict`。
- Produces: `_apply_local_config(cfg) -> None` 更新现有运行参数；`init(C)` 在 RPC 无法启动时继续完成其他调度。

- [ ] **Step 1: 写启动接线失败测试**

```python
def test_init_applies_local_config_and_starts_rpc(monkeypatch):
    C = Mock()
    cfg = {"share_dir": r"D:\runtime",
           "rpc": {"enable": True, "host": "127.0.0.1", "port": 60001,
                   "token": "x" * 32, "allow_trade": True,
                   "allow_ips": ["127.0.0.1"]},
           "live_push": {"enable": False, "period": "5nSecond"}}
    monkeypatch.setattr(rpc_server, "load_local_config", lambda: cfg)
    serve = Mock(return_value=Mock())
    monkeypatch.setattr(rpc_server, "serve", serve)
    rpc_server.init(C)
    serve.assert_called_once_with(rpc_server.__dict__, C,
        host="127.0.0.1", port=60001, token="x" * 32,
        allow_trade=True, allow_ips=["127.0.0.1"])
```

- [ ] **Step 2: 写 RPC 局部失败测试**

```python
def test_init_keeps_schedules_when_rpc_config_is_invalid(monkeypatch, capsys):
    C = Mock()
    monkeypatch.setattr(rpc_server, "load_local_config",
                        Mock(side_effect=rpc_server.QmtConfigError("rpc.port 非法")))
    rpc_server.init(C)
    names = [call.args[0] for call in C.run_time.call_args_list]
    assert names[:2] == ["do_rebalance", "do_snapshot"]
    assert "RPC 未启动" in capsys.readouterr().out
```

- [ ] **Step 3: 运行启动测试确认失败**

Run: `python3 -m pytest tests/test_qmt_bridge.py -k "init_applies or init_keeps" -q`

Expected: FAIL，现有 `init` 尚未加载和显式传递配置。

- [ ] **Step 4: 接入启动配置**

调整 `init(C)`：先尝试加载配置；成功后一次性应用 `SHARE_DIR`、RPC 和实盘推送
参数；所有调度使用该启动快照。调用 `serve` 时显式传入 host、port、token、
allow_trade、allow_ips，避免函数默认参数捕获旧值。配置解析或 RPC bind 失败时，
打印 `[rpc] RPC 未启动:<安全原因>` 并保持 `_RPC_SERVER = None`，不重新抛出。

- [ ] **Step 5: 运行桥接测试**

Run: `python3 -m pytest tests/test_qmt_bridge.py -q`

Expected: PASS。

- [ ] **Step 6: 提交启动接线**

```bash
git add qmt_shell/sinan_qmt.py tests/test_qmt_bridge.py
git commit -m "feat: apply qmt config at strategy startup"
```

### Task 3: 示例配置、部署说明与完整验证

**Files:**
- Create: `qmt_shell/qmt.local.example.json`
- Modify: `docs/qmt-deploy.md`
- Modify: `qmt_shell/sinan_qmt.py:1-70`
- Test: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: `C:\sinan\config\qmt.json` 配置契约。
- Produces: 可复制的无秘密 JSON、一次性 PowerShell 初始化命令和更新后的脚本说明。

- [ ] **Step 1: 添加无秘密示例文件**

```json
{
  "share_dir": "C:\\sinan\\var\\runtime",
  "rpc": {
    "enable": true,
    "host": "0.0.0.0",
    "port": 58620,
    "token": "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS",
    "allow_trade": true,
    "allow_ips": ["REPLACE_WITH_CLIENT_PUBLIC_IP"]
  },
  "live_push": {"enable": true, "period": "5nSecond"}
}
```

- [ ] **Step 2: 更新脚本头部和部署文档**

删除“唯一必填 SHARE_DIR”的旧说明，改为“机器配置只在
`C:\sinan\config\qmt.json` 维护一次”。文档加入创建目录、生成 32 字节随机 Token、
写入 JSON、限制 ACL、把同一 Token 写入 macOS `~/.qmt_rpc_token`、重启策略和在
设置页点击“验证 RPC”的完整 PowerShell/macOS 命令。

- [ ] **Step 3: 添加秘密不落源码的静态测试**

```python
def test_qmt_script_uses_external_machine_config():
    source = Path(rpc_server.__file__).read_text(encoding="utf-8")
    assert 'RPC_TOKEN = ""' not in source
    assert "120.245.101.210" not in source
    assert r"C:\sinan\config\qmt.json" in source
```

- [ ] **Step 4: 运行专项与全量验证**

Run: `python3 -m pytest tests/test_qmt_bridge.py tests/test_qmt_rpc_readiness.py -q`

Expected: PASS。

Run: `python3 -m pytest tests/ -q`

Expected: 全量 PASS。

Run: `python3 -m py_compile qmt_shell/sinan_qmt.py`

Expected: exit code 0，确认语法兼容；同时人工检查未使用 Python 3.7+ 语法。

- [ ] **Step 5: 提交文档和示例配置**

```bash
git add qmt_shell/qmt.local.example.json qmt_shell/sinan_qmt.py \
  docs/qmt-deploy.md tests/test_qmt_bridge.py
git commit -m "docs: add qmt local config setup"
```
