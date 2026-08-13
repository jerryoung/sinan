# QMT RPC Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为实盘配置增加无下单副作用的 QMT RPC 就绪验证。

**Architecture:** QMT 服务端提供内建健康协议；本地验证器按独立连接检查网络、协议、行情和账户；Streamlit 页面只负责编排和展示。

**Tech Stack:** Python socket、QMT RPC JSON 协议、Streamlit、pytest。

## Global Constraints

- `RPC_ALLOW_TRADE = True` 为脚本默认值。
- 健康检查不得调用 `passorder`、`cancel` 或其他交易函数。
- Token 只从 `~/.qmt_rpc_token` 读取。
- 每个功能检查使用独立 RPC 连接。

### Task 1: 健康协议

**Files:** `qmt_shell/sinan_qmt.py`, `qmt_shell/qmt_sdk.py`, `tests/test_qmt_bridge.py`

- [ ] 写默认 trade 与健康协议失败测试并确认失败。
- [ ] 实现 `__sinan_health__` 和 SDK `health()`。
- [ ] 运行 `tests/test_qmt_bridge.py` 确认通过。

### Task 2: 就绪验证器

**Files:** `sinan/live/qmt_health.py`, `tests/test_qmt_health.py`

- [ ] 用真实本地 socket 服务写端到端失败测试。
- [ ] 实现 TCP、协议、行情、账户的独立连接验证。
- [ ] 确认验证报告包含 `ready`、`trade_enabled` 和逐项结果。

### Task 3: 设置页接线

**Files:** `ui/live_profiles.py`, `tests/test_ui_settings.py`, `docs/qmt-deploy.md`

- [ ] 写按钮和结果渲染接线失败测试。
- [ ] 在当前表单配置上执行验证并展示五项状态。
- [ ] 更新部署文档，运行全量测试并提交。
