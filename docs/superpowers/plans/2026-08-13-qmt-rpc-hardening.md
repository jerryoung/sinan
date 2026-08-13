# QMT RPC Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付不预填 Token 和白名单、Windows 单实例监听、默认只读且诊断信息完整的大 QMT 单脚本。

**Architecture:** 保持 `qmt_shell/sinan_qmt.py` 的文件桥接和 RPC 协议不变，只收紧 RPC 配置与 socket 创建边界。通过 `_make_server_socket()` 隔离平台差异，`serve()` 负责安全校验、运行时诊断和线程启动。

**Tech Stack:** Python 标准库 socket/threading/ipaddress、pytest、QMT 内置 Python。

## Global Constraints

- `RPC_TOKEN = ""`、`RPC_ALLOW_IPS = []`，不预填用户秘密或网络地址。
- `RPC_ALLOW_TRADE = False`，远端 RPC 默认只读。
- 非本机绑定且缺少 Token 或白名单时拒绝启动。
- Windows 使用 `SO_EXCLUSIVEADDRUSE`；其他平台使用 `SO_REUSEADDR`。
- 保留 targets、fills、虚拟账本和 5 秒实盘推送能力。

---

### Task 1: 锁定安全配置与独占监听

**Files:**
- Modify: `tests/test_qmt_bridge.py`
- Modify: `qmt_shell/sinan_qmt.py`

**Interfaces:**
- Produces: `_make_server_socket() -> socket.socket`
- Consumes: `serve(namespace, C, host, port, token, allow_trade, allow_ips)`

- [ ] **Step 1: Write the failing tests**

增加断言：默认 `RPC_TOKEN` 和 `RPC_ALLOW_IPS` 为空、`RPC_ALLOW_TRADE` 为 False；模拟 Windows socket 时必须调用 `SO_EXCLUSIVEADDRUSE`，不能调用 `SO_REUSEADDR`。

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest tests/test_qmt_bridge.py -q`

Expected: 默认交易开关与 `_make_server_socket` 测试失败。

- [ ] **Step 3: Implement the minimal production changes**

将默认交易开关设为 False；增加 `_make_server_socket()`；`serve()` 使用该函数并打印脚本路径、白名单、Token 长度和连接来源。

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `python -m pytest tests/test_qmt_bridge.py -q`

Expected: PASS。

### Task 2: 合并完整 QMT 单脚本能力并验证

**Files:**
- Modify: `qmt_shell/sinan_qmt.py`
- Modify: `docs/qmt-deploy.md`

**Interfaces:**
- Preserves: `init(C)`, `handlebar(C)`, `do_rebalance(C)`, `do_snapshot(C)`, `do_live_push(C)`, `serve(...)`。

- [ ] **Step 1: Merge the complete script baseline**

从用户提供的完整脚本补回实盘推送能力，但配置保持 Global Constraints 的空值和默认只读语义，不复制附件中的凭证或白名单。

- [ ] **Step 2: Update deployment instructions**

说明只有一个 QMT 模型可启用 RPC、Windows 独占端口、需要在顶部配置区手工填写 Token/白名单。

- [ ] **Step 3: Run full verification**

Run: `PYTHONPATH=/Users/jaryoung/project/trader /opt/anaconda3/bin/python -m pytest tests/ -q && git diff --check`

Expected: 全部测试通过且 diff 无空白错误。

- [ ] **Step 4: Commit**

Run: `git add qmt_shell/sinan_qmt.py tests/test_qmt_bridge.py docs/qmt-deploy.md docs/superpowers/plans/2026-08-13-qmt-rpc-hardening.md && git commit -m "fix: harden qmt rpc single-instance shell"`
