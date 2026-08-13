# QMT RPC Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 默认开启 QMT RPC 交易能力，并在实盘配置页提供无副作用的分层就绪验证。

**Architecture:** QMT 协议内建 `rpc.health`，本地 `sinan/live/qmt_rpc.py` 负责 TCP、健康协议和行情三层验证，Streamlit 页面仅编排并展示结构化结果。验证路径不调用任何交易函数。

**Tech Stack:** Python socket、QMT RPC JSON 协议、Streamlit、pytest。

## Global Constraints

- `RPC_ALLOW_TRADE = True`。
- 验证不调用 `passorder`、`cancel` 或其他交易函数。
- Token 仅从 `~/.qmt_rpc_token` 读取，不写入配置、日志或页面状态。
- 验证使用当前表单的 host、port、timeout，无需先保存。

---

### Task 1: 健康协议

**Files:**
- Modify: `tests/test_qmt_bridge.py`
- Modify: `qmt_shell/sinan_qmt.py`
- Modify: `qmt_shell/qmt_sdk.py`

**Interfaces:**
- Produces: `qmt_sdk.health() -> dict`
- Protocol: `rpc.health`

- [ ] 写默认交易权限和健康响应失败测试。
- [ ] 运行 `pytest tests/test_qmt_bridge.py -q` 确认 RED。
- [ ] 实现 `rpc.health` 与 SDK 包装。
- [ ] 再运行桥接测试确认 GREEN。

### Task 2: 本地就绪验证器

**Files:**
- Create: `sinan/live/qmt_rpc.py`
- Create: `tests/test_qmt_rpc_readiness.py`

**Interfaces:**
- Produces: `verify_qmt_rpc(rpc: QmtRpcCfg, token_path: Path | None = None) -> QmtRpcReadiness`

- [ ] 写成功、TCP 失败、Token 失败和行情失败测试。
- [ ] 运行新测试确认 RED。
- [ ] 实现结构化结果和分层独立连接。
- [ ] 运行新测试确认 GREEN。

### Task 3: 设置页接线与验收

**Files:**
- Modify: `ui/live_profiles.py`
- Modify: `tests/test_ui_settings.py`
- Modify: `docs/qmt-deploy.md`

**Interfaces:**
- Consumes: `verify_qmt_rpc(...)`

- [ ] 写页面按钮和结果字段静态失败测试。
- [ ] 实现“验证 RPC”按钮及成功/失败反馈。
- [ ] 运行 UI 定向测试。
- [ ] 在浏览器验证远程配置的交互与反馈。
- [ ] 运行 `pytest tests/ -q`、`py_compile` 和 `git diff --check`。
- [ ] 提交 `feat: add qmt rpc readiness verification`。
