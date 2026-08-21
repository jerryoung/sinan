# QMT RPC Live Execution Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable QMT RPC bridge that publishes targets idempotently, tracks real order/deal state, writes fills only from actual deals, and proves the complete flow in the bound simulation account.

**Architecture:** Keep `generate_targets` and local targets files unchanged. Add narrow RPC v2 methods for target publication and execution status, persist an execution journal on the QMT machine, and rebuild each strategy ledger from a persisted baseline plus deduplicated QMT deals. Route QMT C++ API calls through a scheduled QMT-thread pump while keeping pure health/file RPC methods in socket workers.

**Tech Stack:** Python 3.6-compatible QMT shell, Python 3 local client, JSON-over-TCP, `threading`/`queue`, Pydantic configuration, pytest, Streamlit.

**Spec:** `docs/superpowers/specs/2026-08-21-qmt-rpc-live-execution-design.md`

## Global Constraints

- `generate_targets` remains the single pure strategy implementation for backtest and live execution.
- The QMT script must remain one pasteable file and run on QMT's Python 3.6 runtime.
- The targets weight checksum algorithm must remain byte-for-byte identical on both sides.
- Token values stay in `C:\sinan\config\qmt.json` and `~/.qmt_rpc_token`; never write them to repository files, logs, targets, fills, or execution journals.
- A `passorder` call is not a fill; only QMT deal records may mutate strategy cash, positions, or `fills`.
- Uncertain submission is at-most-once: stop and alert instead of automatically submitting again.
- `run_signal.py` remains non-trading and never publishes targets remotely.
- Preserve the user's uncommitted `config/live_profiles.yaml`; never stage `.workbuddy/`.
- Current public RPC endpoint is authorized only for the simulation-account validation in this task; real-money use requires SSH or Tailscale.

---

### Task 1: Make QMT object serialization and socket shutdown safe

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:246-305,543-635`
- Modify: `qmt_shell/qmt_sdk.py:48-104`
- Modify: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: existing `to_jsonable(obj, _depth=0)` and `_handle(...)` RPC behavior.
- Produces: `_parse_iso_datetime(value: str) -> datetime`, safe `to_jsonable`, and orderly `_Client.close()`.

- [ ] **Step 1: Write failing compatibility and serialization tests**

Add tests that model the exact QMT failure and Python 3.6 constraint:

```python
class _BadOrderTag:
    m_strRemark = "probe#20260821#1"
    m_strOrderSysID = "12345"

    @property
    def m_xtTag(self):
        raise TypeError("No to_python converter for CXtOrderTag")


def test_to_jsonable_skips_unconvertible_qmt_attribute():
    out = rpc_server.to_jsonable(_BadOrderTag())
    assert out["m_strRemark"] == "probe#20260821#1"
    assert out["m_strOrderSysID"] == "12345"
    assert "m_xtTag" not in out


def test_qmt_iso_parser_is_python36_compatible():
    value = rpc_server._parse_iso_datetime("2026-08-21T14:35:01")
    assert value.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-21 14:35:01"
    source = Path(rpc_server.__file__).read_text(encoding="utf-8")
    assert "datetime.fromisoformat" not in source
```

Add a fake connection whose `recv` raises `ConnectionAbortedError(10053, ...)`; call
`_handle` and assert it returns without printing a traceback. Add a fake reader/socket to
assert `_Client.close()` closes the reader, calls `shutdown(socket.SHUT_RDWR)`, then closes
the socket.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_bridge.py -q`

Expected: failures for the unconvertible property, missing `_parse_iso_datetime`, uncaught
disconnect, and incomplete close sequence.

- [ ] **Step 3: Implement the minimal compatibility fix**

Implement parsing without Python 3.7 APIs:

```python
def _parse_iso_datetime(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    raise ValueError("ISO 时间格式错误: %s" % text)
```

Refactor the object branch of `to_jsonable` to read each `m_*` field in its own
`try/except Exception`, skip only the failed field, and preserve all readable fields. Catch
peer-disconnect exceptions around both `recv` and `sendall`. In `_Client.close`, close
`_rf`, attempt `shutdown(SHUT_RDWR)`, then close `_sock`, swallowing only `OSError`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_bridge.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the compatibility fix**

```bash
git add qmt_shell/sinan_qmt.py qmt_shell/qmt_sdk.py tests/test_qmt_bridge.py
git commit -m "fix: harden qmt rpc serialization"
```

---

### Task 2: Route QMT API calls through a scheduled request pump

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:543-710`
- Modify: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: `dispatch(namespace, C, fn, args, kwargs, allow_trade)` and QMT
  `C.run_time(function_name, period, start, market)`.
- Produces: `_submit_api_request(...)`, `do_rpc_pump(C)`, protocol v2 health with
  `capabilities`, and bounded `_RPC_API_QUEUE`.

- [ ] **Step 1: Write failing queue and protocol tests**

Add tests proving that a worker blocks until the QMT pump executes the request:

```python
def test_qmt_api_request_runs_only_when_pump_drains_queue(monkeypatch):
    seen = []
    monkeypatch.setattr(rpc_server, "_RPC_NAMESPACE", {
        "get_trade_detail_data": lambda *args: seen.append(args) or ["ok"]
    })
    rpc_server._reset_rpc_queue_for_test()
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(value=rpc_server._submit_api_request(
            "get_trade_detail_data", ["a", "STOCK", "order"], {}, 1.0
        ))
    )
    worker.start()
    assert seen == []
    rpc_server.do_rpc_pump(_FakeC())
    worker.join(1)
    assert result["value"] == ["ok"]
    assert seen == [("a", "STOCK", "order")]
```

Add tests for queue-full and request-timeout errors, direct `rpc.health`, protocol `2`, and
the capability set:

```python
assert health["protocol"] == 2
assert set(health["capabilities"]) == {"qmt_api_queue"}
```

Add allowlist tests for the QMT methods used by this repository. Permit
`get_trade_detail_data`, `get_last_order_id`, `get_value_by_order_id`,
`timetag_to_datetime`, `passorder`, `cancel`, `cancel_task`, and these ContextInfo methods:
`get_full_tick`, `get_stock_name`, `get_market_data_ex`, `get_trading_dates`,
`get_stock_list_in_sector`, `get_instrument_detail`. Reject any other global or `C.*` name
before queueing. Add a connection test proving a request line over 1 MiB is rejected without
being parsed or queued.

Update init tests to require `do_rpc_pump` at `1nSecond` in addition to the existing
rebalance, snapshot, and live-push schedules.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_bridge.py -q`

Expected: failures for missing queue functions, protocol v2, capabilities, and schedule.

- [ ] **Step 3: Implement the bounded QMT-thread pump**

Use Python 3.6 `queue.Queue(maxsize=64)` and dictionary request records containing
`fn`, `args`, `kwargs`, `event`, `result`, `error`, and `deadline`. Pure reserved RPC methods
execute in the socket worker; all QMT API names are queued. `do_rpc_pump` processes at most
eight non-expired requests per callback and stores either the returned value or formatted
exception before setting the event.

Register:

```python
C.run_time("do_rpc_pump", "1nSecond", "2026-01-01 00:00:00", "SH")
```

Set protocol to `2` and advertise only `qmt_api_queue` in this task. Later tasks add their
capability names in the same commit that makes each endpoint callable. Preserve
`allow_trade` enforcement before queuing a trade function. Enforce the explicit global and
ContextInfo allowlists before queueing, and cap the newline-delimited request buffer at 1 MiB.
Never retry queued trade calls.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_bridge.py -q`

Expected: all tests pass with queue state reset between tests.

- [ ] **Step 5: Commit the request-pump change**

```bash
git add qmt_shell/sinan_qmt.py tests/test_qmt_bridge.py
git commit -m "feat: run qmt rpc calls on strategy thread"
```

---

### Task 3: Add idempotent targets publication and the local bridge

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:270-305,562-580`
- Modify: `qmt_shell/qmt_sdk.py`
- Create: `sinan/live/qmt_bridge.py`
- Create: `scripts/publish_targets.py`
- Create: `tests/test_qmt_target_publish.py`
- Modify: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: payloads produced by `sinan.live.targets.build_payload`,
  `LiveProfileCfg.qmt.rpc`, and `~/.qmt_rpc_token`.
- Produces: server `_publish_targets(payload, now=None) -> dict`, SDK
  `publish_targets(payload) -> dict`, `QmtRpcBridge.publish(payload) -> PublishResult`, and
  CLI `scripts/publish_targets.py TARGET_FILE`.

- [ ] **Step 1: Write failing server publication tests**

Cover valid publication, duplicate publication, replacement before execution, checksum
mismatch, malformed strategy, wrong date shape, oversized payload, and path traversal:

```python
def test_publish_targets_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    payload = _payload(strategy="alpha", date="2026-08-21")
    first = rpc_server._publish_targets(payload)
    second = rpc_server._publish_targets(payload)
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    files = list((tmp_path / "targets").glob("*.json"))
    assert len(files) == 1
```

Assert that `rpc.publish_targets` is rejected when `allow_trade=False` and that the server
constructs the filename itself.

- [ ] **Step 2: Run publication tests and verify RED**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_target_publish.py tests/test_qmt_bridge.py -q`

Expected: failures for missing publication endpoint and client bridge.

- [ ] **Step 3: Implement server validation and atomic persistence**

Implement `_validate_target_payload`, `_safe_strategy`, `_atomic_write_json`, and
`_publish_targets`. Strategy names may contain Unicode but must reject control characters and
`\\/:*?"<>|`; date must match `YYYY-MM-DD`; checksum must equal `_checksum(targets)`.
Write to a temporary sibling then `os.replace`; the protocol-wide 1 MiB request cap from
Task 2 bounds payload size.

Dispatch `rpc.publish_targets` locally in the socket worker after token and `allow_trade`
checks. Return only status, strategy, date, checksum, and basename—never absolute server
paths. Add `publish_targets` to health capabilities in this same task.

- [ ] **Step 4: Implement the local bridge and explicit CLI**

Define immutable results and an injectable client factory:

```python
@dataclass(frozen=True)
class PublishResult:
    status: str
    strategy: str
    date: str
    checksum: str
    filename: str


class QmtRpcBridge:
    def __init__(self, rpc: QmtRpcCfg, token_path: Path | None = None,
                 client_factory=qmt_sdk._Client): ...
    def publish(self, payload: dict) -> PublishResult: ...
    def execution_status(self, strategy: str, date: str) -> dict: ...
    def pull_fills(self, strategy: str, date: str, fills_dir: Path) -> Path | None: ...
```

`scripts/publish_targets.py` accepts exactly one target path plus optional `--pull`; it reads
`live_profile` from the payload, resolves that exact profile without default fallback, prints
the structured publication result, and exits non-zero on validation or RPC failure.

- [ ] **Step 5: Add local bridge and CLI tests**

Use a fake `_Client` to assert host, port, timeout, token-file loading, exact profile
resolution, no default fallback, payload forwarding, and atomic local fills write. Import
`scripts.run_signal` and assert it contains no `publish_targets` or `QmtRpcBridge` call.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_target_publish.py tests/test_qmt_bridge.py tests/test_run_signal_live_profile.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit targets publication**

```bash
git add qmt_shell/sinan_qmt.py qmt_shell/qmt_sdk.py sinan/live/qmt_bridge.py scripts/publish_targets.py tests/test_qmt_target_publish.py tests/test_qmt_bridge.py tests/test_run_signal_live_profile.py
git commit -m "feat: publish targets through qmt rpc"
```

---

### Task 4: Persist an at-most-once execution journal

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:319-420`
- Create: `tests/test_qmt_execution_journal.py`
- Modify: `tests/test_qmt_bridge.py`

**Interfaces:**
- Consumes: validated target payload, `plan_orders`, strategy ledger, deterministic
  `make_remark`.
- Produces: `_execution_path`, `load_execution`, `save_execution`,
  `prepare_execution(payload, prices)`, `submit_execution(C, execution)`, and persisted
  states `received|planned|submitting|submitted|uncertain`.

- [ ] **Step 1: Write failing journal transition tests**

Create tests for baseline persistence, deterministic order sequence, atomic save, same-target
resume, checksum conflict, and crash-window handling:

```python
def test_passorder_is_preceded_by_submitting_journal(tmp_path, monkeypatch):
    writes = []
    calls = []
    monkeypatch.setattr(rpc_server, "SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(rpc_server, "save_execution",
                        lambda value: writes.append(copy.deepcopy(value)))
    monkeypatch.setattr(rpc_server, "passorder",
                        lambda *args: calls.append(args))
    execution = _planned_execution()
    rpc_server.submit_execution(_FakeC(), execution)
    assert writes[0]["orders"][0]["status"] == "submitting"
    assert len(calls) == 1
    assert writes[-1]["orders"][0]["status"] == "submitted"
```

Simulate `passorder` raising after the `submitting` save and assert status becomes
`uncertain`, the exception is isolated, and a second call does not submit again.
Create a published targets file with a different checksum after the journal reaches
`submitting` and assert `_publish_targets` rejects the replacement.

- [ ] **Step 2: Run journal tests and verify RED**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_execution_journal.py -q`

Expected: failures for missing journal functions and current eager ledger mutation.

- [ ] **Step 3: Implement journal creation and atomic transitions**

Store journals at `SHARE_DIR/executions/execution_{strategy}_{YYYYMMDD}.json`. Persist the
baseline ledger and planned order list before any side effect. For each order, save
`submitting`, invoke `passorder` once, then save `submitted`. If the call raises, save
`uncertain` with exception type/message and continue to the next strategy, not the next order
of the same strategy.

Refactor `do_rebalance` to create or resume journals. It must not mutate/save the strategy
ledger and must not call `_write_fills` after submission. An existing journal with the same
checksum is reconciled, never replanned; a conflicting checksum is rejected.

- [ ] **Step 4: Run journal and existing bridge tests**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_execution_journal.py tests/test_qmt_bridge.py -q`

Expected: all tests pass and existing `plan_orders` behavior stays unchanged.

- [ ] **Step 5: Commit the execution journal**

```bash
git add qmt_shell/sinan_qmt.py tests/test_qmt_execution_journal.py tests/test_qmt_bridge.py
git commit -m "feat: journal qmt order submission"
```

---

### Task 5: Reconcile real orders/deals and write truthful fills

**Files:**
- Modify: `qmt_shell/sinan_qmt.py:423-540`
- Create: `tests/test_qmt_execution_reconcile.py`
- Modify: `tests/test_ledger.py`
- Modify: `tests/test_reconcile_wiring.py`

**Interfaces:**
- Consumes: persisted execution baseline/orders, QMT order/deal objects, strategy remark.
- Produces: `_collect_orders`, `_collect_deals`, `_refresh_execution`,
  `_rebuild_ledger(baseline, deals)`, v2 fills with `execution_status`, `orders`, and actual
  `fills`, plus `rpc.execution_status`.

- [ ] **Step 1: Write failing truth-source tests**

Cover no deals, rejection, cancellation, partial fill, full fill, duplicate deal polling,
restart from disk, unreadable required fields, and per-strategy exception isolation:

```python
def test_submitted_order_without_deal_does_not_change_ledger(tmp_path, monkeypatch):
    baseline = {"cash": 100000.0, "pos": {}}
    execution = _execution(baseline=baseline, orders=[_submitted_order()])
    result = rpc_server._reconcile_execution(
        execution, qmt_orders=[_order(status=50)], qmt_deals=[]
    )
    assert result["ledger"] == baseline
    assert result["fills"] == []


def test_duplicate_deal_is_applied_once():
    deal = _deal(trade_id="D1", qty=100, price=4.8)
    ledger, unique = rpc_server._rebuild_ledger(
        {"cash": 1000.0, "pos": {}}, [deal, deal]
    )
    assert ledger == {"cash": 520.0, "pos": {"510300": 100}}
    assert len(unique) == 1
```

Assert that a zero-deal execution still writes a fills file with unchanged weights and
`fills=[]`, so next-day reconciliation can detect the target deviation.

- [ ] **Step 2: Run reconciliation tests and verify RED**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_execution_reconcile.py tests/test_ledger.py tests/test_reconcile_wiring.py -q`

Expected: failures because current code treats placed orders as fills and relies on in-memory
`_MORNING_LEDGERS`.

- [ ] **Step 3: Implement order/deal normalization and idempotent replay**

Normalize only required safe fields. Use `m_strTradeID` as the primary deal key; if absent,
use `(order_sys_id, remark, symbol, side, qty, price, trade_time)`. Map QMT terminal statuses
to `filled`, `canceled`, and `rejected`, while preserving the raw integer status.

Rebuild from the persisted baseline on every refresh, apply each unique deal once, save the
ledger, and atomically write fills. Replace `_MORNING_LEDGERS` with disk-backed baseline data.
`do_live_push` and `do_snapshot` iterate all current-day execution journals, including those
with zero deals. Catch and report exceptions per strategy.

- [ ] **Step 4: Implement `rpc.execution_status`**

Return a JSON object containing the journal and optional fills for an exact validated strategy
and date. Return `found=False` when neither exists; do not expose arbitrary paths or files.
Keep this endpoint read-only even when `allow_trade=False`, and add `execution_status` to
health capabilities in this same task.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_execution_reconcile.py tests/test_qmt_execution_journal.py tests/test_ledger.py tests/test_reconcile_wiring.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit truthful execution accounting**

```bash
git add qmt_shell/sinan_qmt.py tests/test_qmt_execution_reconcile.py tests/test_ledger.py tests/test_reconcile_wiring.py
git commit -m "fix: derive qmt fills from actual deals"
```

---

### Task 6: Expand readiness and add an explicit simulation trade probe

**Files:**
- Modify: `sinan/live/qmt_rpc.py`
- Modify: `ui/live_profiles.py:138-166`
- Create: `scripts/qmt_trade_probe.py`
- Modify: `tests/test_qmt_rpc_readiness.py`
- Modify: `tests/test_ui_settings.py`
- Create: `tests/test_qmt_trade_probe.py`

**Interfaces:**
- Consumes: protocol v2 health, quote, account/order/deal query, `qmt_sdk.passorder`, and
  `qmt_sdk.cancel`.
- Produces: readiness stages `tcp|health|quote|account|trade_query|ready`, truthful UI labels,
  and a side-effecting CLI guarded by exact simulation-account confirmation.

- [ ] **Step 1: Write failing readiness tests**

Extend the fake client so readiness queries account, order, and deal after quote. Assert that
an order/deal serialization error produces `stage="trade_query"`, account login failure
produces `stage="account"`, and no trade function is called:

```python
assert not any(call[0] in {"passorder", "cancel", "rpc.publish_targets"}
               for call in _FakeClient.calls)
```

- [ ] **Step 2: Write failing UI semantic tests**

Require the labels “RPC 交易转发”, “QMT 模式不可自动检测”, “委托/成交查询”, and
protocol/capability display. Assert the ordinary “验证 RPC” button remains non-trading.

- [ ] **Step 3: Implement layered readiness and UI copy**

Query account using the health-reported account/type, require at least one account object and
an acceptable login status, then query order and deal lists. Store only counts/status in the
readiness result. Change `allow_trade=True` display from “交易权限已开启” to
“RPC 交易转发：允许”; show unknown model mode as unsupported, not ready.

- [ ] **Step 4: Write the failing trade-probe state tests**

Test a pure probe runner with a fake client: exact account confirmation required, one
`passorder` call, condition-based polling by unique remark, cancel by
`m_strOrderSysID`, terminal-status recognition, and no automatic re-submit after timeout.

- [ ] **Step 5: Implement the explicit probe CLI**

Require these arguments:

```text
--confirm-simulation-account 80391000
--symbol 510300.SH
--qty 100
--limit-price <positive price>
```

Generate a remark shorter than 24 characters, call `passorder` once with `quickTrade=2`, poll
orders until the remark appears or timeout expires, then cancel only when status is cancelable.
Print structured JSON without token or unrelated account details. Refuse execution when the
confirmed account differs from `rpc.health.account`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_rpc_readiness.py tests/test_qmt_trade_probe.py tests/test_ui_settings.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit readiness and probe support**

```bash
git add sinan/live/qmt_rpc.py ui/live_profiles.py scripts/qmt_trade_probe.py tests/test_qmt_rpc_readiness.py tests/test_qmt_trade_probe.py tests/test_ui_settings.py
git commit -m "feat: verify qmt trading readiness"
```

---

### Task 7: Update deployment documentation and run the complete local regression

**Files:**
- Modify: `docs/qmt-deploy.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Test: `tests/`

**Interfaces:**
- Consumes: all v2 bridge, journal, fills, CLI, and UI behavior from Tasks 1–6.
- Produces: a single deployment procedure and current user-facing contract.

- [ ] **Step 1: Update the deployment guide**

Document protocol v2 capabilities, Python 3.6 compatibility, explicit publish/pull commands,
execution journal paths, true order/deal/fills semantics, model “实盘运行” requirement for a
bound simulation account, rollback, and the public-RPC warning. Remove the old claim that
binding a simulation account alone determines whether `passorder` reaches the counter.

- [ ] **Step 2: Update README and AGENTS contracts**

Describe the new remote path and explicitly state:

```text
targets = intent
orders = submission/counter state
fills = actual deals only
```

Record that `run_signal` never publishes and that publication is an explicit separate action.
Do not change research results or backtest metrics.

- [ ] **Step 3: Run syntax and focused contract checks**

Run:

```bash
/opt/anaconda3/bin/python3 -m py_compile qmt_shell/sinan_qmt.py qmt_shell/qmt_sdk.py sinan/live/qmt_bridge.py sinan/live/qmt_rpc.py scripts/publish_targets.py scripts/qmt_trade_probe.py
/opt/anaconda3/bin/python3 -m pytest tests/test_qmt_bridge.py tests/test_qmt_target_publish.py tests/test_qmt_execution_journal.py tests/test_qmt_execution_reconcile.py tests/test_qmt_rpc_readiness.py tests/test_qmt_trade_probe.py tests/test_ledger.py tests/test_reconcile_wiring.py tests/test_ui_settings.py -q
```

Expected: compilation succeeds and all focused tests pass.

- [ ] **Step 4: Run the full suite**

Run: `/opt/anaconda3/bin/python3 -m pytest tests/ -q`

Expected: all repository tests pass; snapshot NAV fixture remains unchanged.

- [ ] **Step 5: Audit the diff and secrets**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~6..HEAD
rg -n "cf9fb3413925177b03735736a8339787|120\.245\.101\.210|8\.155\.45\.134" --glob '!config/live_profiles.yaml' --glob '!.workbuddy/**' .
```

Expected: no token or user IP is introduced; the public endpoint remains only in the user's
existing uncommitted profile; `.workbuddy/` is untracked and unstaged.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md AGENTS.md docs/qmt-deploy.md docs/superpowers/plans/2026-08-21-qmt-rpc-live-execution.md
git commit -m "docs: document qmt rpc execution lifecycle"
```

---

### Task 8: Deploy to QMT and prove the simulation-account flow

**Files:**
- Deploy: `qmt_shell/sinan_qmt.py` into the existing QMT model editor
- Preserve: `C:\sinan\config\qmt.json`
- Observe: QMT logs and local structured probe output

**Interfaces:**
- Consumes: tested local artifacts from Tasks 1–7 and the user-confirmed QMT model running in
  “实盘运行” while bound to simulation account `80391000`.
- Produces: live evidence for quote, account, order, cancel/deal, target publication, journal,
  fills, restart idempotency, and measured latency.

- [ ] **Step 1: Provide and deploy the complete QMT script**

Copy the entire tested `qmt_shell/sinan_qmt.py` into the one QMT model. Stop and start that
model; do not restart or modify unrelated strategies. Confirm logs show one listener,
protocol-v2 code, account `80391000`, and `trade=True` without token content.

- [ ] **Step 2: Run read-only readiness**

Run the local readiness path and record health, protocol, capabilities, quote, account login,
position count, order count, deal count, and each stage latency. Expected: every stage is
ready and order/deal serialization succeeds.

- [ ] **Step 3: Run one minimum simulation order probe during market hours**

Resolve a liquid ETF whose 100-share notional plus fees is below available cash. Use a limit
that first permits observing/canceling an open order; execute the guarded probe once. Expected:
the unique remark appears, a system order ID is returned, and cancel reaches a terminal state.

- [ ] **Step 4: Prove an actual simulated deal**

During market hours submit one explicitly confirmed 100-share order at a marketable price in
the simulation account. Expected: exactly one deal with matching remark/order ID, corresponding
cash/position movement, and no second submission. If the instrument is T+1, retain the simulated
position and record it rather than attempting an invalid same-day sell.

- [ ] **Step 5: Publish and reconcile a dedicated target**

Generate a dedicated small-capital test targets payload using the normal targets builder,
publish it twice, and require `accepted` then `duplicate`. Trigger the normal QMT execution
at 14:45 exactly once; no test-only bypass endpoint is permitted. Pull execution status and
fills; assert orders reflect counter state and `fills` contains only actual deals. Run local
`reconcile_fills` against the pulled file and record the expected result.

- [ ] **Step 6: Verify restart recovery**

Stop and start the QMT model once. Re-query the same execution journal and account orders.
Expected: port is released/rebound, no duplicate remark/order/deal appears, and ledger/fills are
identical after idempotent replay.

- [ ] **Step 7: Measure latency and perform completion audit**

Run at least 20 health/quote/account/order calls and report median/P95. Audit every acceptance
item in the design against current files, test output, QMT logs, and account records. Keep the
goal active if market-hours order/deal evidence is unavailable.

- [ ] **Step 8: Commit and push only after all evidence passes**

Run:

```bash
git status --short
git log --oneline origin/main..HEAD
git push origin main
```

Stage no user configuration and no `.workbuddy/` content. Push only the reviewed implementation
commits after the full local suite and available live verification pass.
