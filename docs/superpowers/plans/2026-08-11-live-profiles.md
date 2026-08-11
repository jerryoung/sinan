# 可复用实盘配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可增删改的命名实盘配置，策略只通过 `live_profile` 引用配置，消除 QMT 参数重复配置。

**Architecture:** `config/live_profiles.yaml` 是实盘配置唯一事实来源；`sinan.config` 负责强类型模型、加载、原子保存与解析，`sinan.live.profiles` 负责引用扫描和安全删除。设置页只管理配置集合，策略页只选择配置引用；`run_signal` 把命名配置解析成现有 QMT 薄壳消费的 `qmt` 字段并追加 `live_profile` 留痕。

**Tech Stack:** Python 3.12、Pydantic v2、PyYAML、Streamlit、pytest。

## Global Constraints

- 不保留 `Settings.live.qmt`、`StrategyCfg.qmt` 或策略内联 QMT 的兼容入口。
- `qmt_rpc` 始终是系统级连接配置，不进入命名实盘配置。
- 配置 ID 创建后不可修改；默认配置和被策略引用的配置禁止删除。
- 策略引用不存在时必须拒绝保存和出信号，不允许静默回退。
- targets 保持现有 `qmt` 字段以兼容 QMT 薄壳，并新增 `live_profile` 留痕。
- 当前工作区已有未提交的 QMT 实时监控改动；实施时不得覆盖或误提交这些改动。
- 所有生产代码遵循 RED → GREEN → REFACTOR；每个行为必须先观察到对应测试失败。

---

## File Structure

- Create `config/live_profiles.yaml`: 命名实盘配置数据。
- Modify `sinan/config.py`: 配置模型、唯一键 YAML 加载、原子保存、策略引用解析。
- Create `sinan/live/profiles.py`: 策略引用扫描、安全删除及集合变更原语。
- Modify `sinan/live/targets.py`: payload 增加 `live_profile` 留痕。
- Modify `scripts/run_signal.py`: 从命名配置解析 QMT 参数。
- Create `ui/live_profiles.py`: 实盘配置 CRUD 界面。
- Modify `ui/settings_page.py`: 系统设置/实盘配置页签，删除旧全局 QMT 参数入口。
- Modify `ui/common.py`: 策略实盘配置下拉框和只读摘要。
- Modify `ui/strategy_config.py`: 保存前验证策略引用。
- Modify `config/strategies/*.yaml`: 显式引用 `local_qmt`。
- Replace `tests/test_live_cfg.py` with `tests/test_live_profiles.py`: 配置模型和解析测试。
- Create `tests/test_live_profile_crud.py`: 引用扫描与删除测试。
- Modify `tests/test_targets.py`: payload 留痕测试。
- Modify `tests/test_ui_settings.py`: 单一配置入口结构测试。
- Modify `README.md` and `AGENTS.md`: 用户流程与架构约定。

---

### Task 1: 强类型实盘配置模型与唯一事实来源

**Files:**
- Create: `config/live_profiles.yaml`
- Modify: `sinan/config.py`
- Replace: `tests/test_live_cfg.py` → `tests/test_live_profiles.py`

**Interfaces:**
- Produces: `QmtAlgoCfg`, `QmtExecutionCfg`, `LiveProfileCfg`, `LiveProfilesCfg`
- Produces: `load_live_profiles(path: str | Path | None = None) -> LiveProfilesCfg`
- Produces: `save_live_profiles(cfg: LiveProfilesCfg, path: str | Path | None = None) -> Path`
- Produces: `resolve_live_profile(profiles: LiveProfilesCfg, cfg: StrategyCfg) -> tuple[str, LiveProfileCfg]`
- Changes: `StrategyCfg.live_profile: str = "local_qmt"`; removes `StrategyCfg.qmt`
- Changes: removes `LiveCfg` and `Settings.live`

- [ ] **Step 1: Write failing model and resolver tests**

```python
def test_repo_live_profiles_has_local_qmt():
    cfg = load_live_profiles()
    assert cfg.default == "local_qmt"
    assert cfg.profiles["local_qmt"].engine == "qmt"
    assert cfg.profiles["local_qmt"].qmt.algo.quote_mode == "latest"

def test_default_must_reference_existing_profile():
    with pytest.raises(ValueError, match="default"):
        LiveProfilesCfg(default="missing", profiles={"local_qmt": _profile()})

def test_profile_id_is_restricted():
    with pytest.raises(ValueError, match="配置 ID"):
        LiveProfilesCfg(default="Local QMT", profiles={"Local QMT": _profile()})

def test_duplicate_yaml_profile_id_is_rejected(tmp_path):
    path = tmp_path / "live_profiles.yaml"
    path.write_text("default: a\nprofiles:\n  a: {name: A, engine: qmt}\n  a: {name: B, engine: qmt}\n")
    with pytest.raises(ValueError, match="重复键.*a"):
        load_live_profiles(path)

def test_strategy_reference_must_exist():
    cfg = StrategyCfg(name="s", strategy="donchian", universe=["510300"],
                      live_profile="missing")
    with pytest.raises(ValueError, match="策略 s.*missing"):
        resolve_live_profile(_profiles(), cfg)

def test_resolver_returns_deep_copy():
    profiles = _profiles()
    _, resolved = resolve_live_profile(profiles, _strategy())
    resolved.qmt.algo.price_offset = 9.9
    assert profiles.profiles["local_qmt"].qmt.algo.price_offset == 0.002
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_live_profiles.py -q`

Expected: FAIL because the new models, loader and resolver do not exist.

- [ ] **Step 3: Implement the minimal models and loader**

```python
class QmtAlgoCfg(BaseModel):
    quote_mode: Literal["latest", "limit"] = "latest"
    price_offset: float = Field(default=0.002, ge=0)
    max_order_qty: int = Field(default=10000, gt=0)

class QmtExecutionCfg(BaseModel):
    account: str | None = None
    algo: QmtAlgoCfg = Field(default_factory=QmtAlgoCfg)

class LiveProfileCfg(BaseModel):
    name: str = Field(min_length=1)
    engine: Literal["qmt"] = "qmt"
    qmt: QmtExecutionCfg = Field(default_factory=QmtExecutionCfg)

class LiveProfilesCfg(BaseModel):
    default: str
    profiles: dict[str, LiveProfileCfg]

    @model_validator(mode="after")
    def _validate_refs(self):
        if not self.profiles:
            raise ValueError("实盘配置不能为空")
        for profile_id in self.profiles:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", profile_id):
                raise ValueError(f"实盘配置 ID 不合法:{profile_id}")
        if self.default not in self.profiles:
            raise ValueError(f"default 指向不存在的实盘配置:{self.default}")
        return self
```

Use a `yaml.SafeLoader` subclass whose mapping constructor rejects duplicate keys. Implement
`save_live_profiles` by writing UTF-8 YAML to a sibling temporary file, flushing, then calling
`Path.replace()`; delete the temporary file on failure without touching the original.

- [ ] **Step 4: Add the repository default profile and remove old model fields**

Create `config/live_profiles.yaml` exactly as specified in the design. Add
`StrategyCfg.live_profile = "local_qmt"`; remove `LiveCfg`, `Settings.live`, `check_qmt`,
`resolve_qmt`, and `StrategyCfg.qmt`.

- [ ] **Step 5: Run focused and configuration tests**

Run: `python3 -m pytest tests/test_live_profiles.py tests/test_core_contracts.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 1 only**

```bash
git add config/live_profiles.yaml sinan/config.py tests/test_live_profiles.py tests/test_live_cfg.py
git commit -m "feat: add named live profile model"
```

---

### Task 2: 引用扫描与禁止删除

**Files:**
- Create: `sinan/live/profiles.py`
- Create: `tests/test_live_profile_crud.py`

**Interfaces:**
- Produces: `StrategyProfileRef(strategy_id: str, display_name: str, path: Path)`
- Produces: `ProfileDeleteBlocked(ValueError)` with `references` and `parse_errors`
- Produces: `find_profile_references(profile_id: str, strategy_dir: Path) -> list[StrategyProfileRef]`
- Produces: `delete_live_profile(cfg: LiveProfilesCfg, profile_id: str, strategy_dir: Path) -> LiveProfilesCfg`
- Produces: `upsert_live_profile(cfg, profile_id, profile) -> LiveProfilesCfg`
- Produces: `set_default_live_profile(cfg, profile_id) -> LiveProfilesCfg`

- [ ] **Step 1: Write failing CRUD tests**

```python
def test_default_profile_cannot_be_deleted(tmp_path):
    with pytest.raises(ProfileDeleteBlocked, match="默认"):
        delete_live_profile(_profiles(), "local_qmt", tmp_path)

def test_referenced_profile_cannot_be_deleted_and_lists_strategy(tmp_path):
    _write_strategy(tmp_path / "alpha.yaml", "alpha", "remote_qmt")
    with pytest.raises(ProfileDeleteBlocked) as exc:
        delete_live_profile(_profiles_with_remote(), "remote_qmt", tmp_path)
    assert exc.value.references[0].strategy_id == "alpha"
    assert exc.value.references[0].path.name == "alpha.yaml"

def test_unparseable_strategy_blocks_delete(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [")
    with pytest.raises(ProfileDeleteBlocked) as exc:
        delete_live_profile(_profiles_with_remote(), "remote_qmt", tmp_path)
    assert exc.value.parse_errors[0].name == "broken.yaml"

def test_unreferenced_non_default_profile_can_be_deleted(tmp_path):
    out = delete_live_profile(_profiles_with_remote(), "remote_qmt", tmp_path)
    assert "remote_qmt" not in out.profiles
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_live_profile_crud.py -q`

Expected: FAIL with missing `sinan.live.profiles`.

- [ ] **Step 3: Implement immutable collection operations**

All operations return a freshly validated `LiveProfilesCfg` via `model_copy(deep=True)`;
none mutates the input. `delete_live_profile` reads every `*.yaml` from disk, rejects default,
collects references, and fails closed on any YAML/Pydantic parse error.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m pytest tests/test_live_profile_crud.py tests/test_live_profiles.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add sinan/live/profiles.py tests/test_live_profile_crud.py
git commit -m "feat: protect referenced live profiles from deletion"
```

---

### Task 3: targets 与 run_signal 接入命名配置

**Files:**
- Modify: `sinan/live/targets.py`
- Modify: `scripts/run_signal.py`
- Modify: `tests/test_targets.py`
- Modify: `tests/test_live_profiles.py`

**Interfaces:**
- Changes: `build_payload(..., live_profile: str | None = None, qmt: dict | None = None)`
- Consumes: `load_live_profiles()` and `resolve_live_profile()`

- [ ] **Step 1: Write failing payload and execution tests**

```python
def test_payload_records_profile_and_keeps_qmt_contract():
    payload = build_payload({}, strategy_name="s", date="2026-08-11",
                            data_cutoff="2026-08-10", params_fingerprint={},
                            live_profile="local_qmt",
                            qmt={"algo": {"quote_mode": "latest"}})
    assert payload["live_profile"] == "local_qmt"
    assert payload["qmt"]["algo"]["quote_mode"] == "latest"

def test_resolved_profile_serializes_for_qmt_shell():
    profile_id, profile = resolve_live_profile(_profiles(), _strategy())
    assert profile_id == "local_qmt"
    assert profile.qmt.model_dump(exclude_none=True)["algo"]["max_order_qty"] == 10000
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_targets.py tests/test_live_profiles.py -q`

Expected: FAIL because `build_payload` does not accept `live_profile`.

- [ ] **Step 3: Implement payload and orchestration wiring**

In `run_signal.main()`:

```python
profiles = load_live_profiles()
profile_id, live_profile = resolve_live_profile(profiles, cfg)
qmt_cfg = live_profile.qmt.model_dump(exclude_none=True)
payload = build_payload(..., live_profile=profile_id, qmt=qmt_cfg)
```

Remove all `resolve_qmt(settings, cfg)` calls. Do not modify `qmt_shell/sinan_qmt.py`.

- [ ] **Step 4: Run focused tests and script syntax check**

Run: `python3 -m pytest tests/test_targets.py tests/test_live_profiles.py -q`

Run: `python3 -m py_compile scripts/run_signal.py sinan/live/targets.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add scripts/run_signal.py sinan/live/targets.py tests/test_targets.py tests/test_live_profiles.py
git commit -m "feat: resolve named live profile when building targets"
```

---

### Task 4: 设置页实盘配置 CRUD

**Files:**
- Create: `ui/live_profiles.py`
- Modify: `ui/settings_page.py`
- Modify: `tests/test_ui_settings.py`

**Interfaces:**
- Consumes: model/load/save APIs from Task 1
- Consumes: CRUD/reference APIs from Task 2
- Produces: `render_live_profiles_page() -> None`

- [ ] **Step 1: Write failing UI-structure tests**

```python
def test_settings_page_has_system_and_live_profile_tabs():
    source = Path("ui/settings_page.py").read_text()
    assert 'st.tabs(["系统设置", "实盘配置"])' in source
    assert "render_live_profiles_page" in source

def test_settings_page_no_longer_edits_inline_qmt():
    source = Path("ui/settings_page.py").read_text()
    assert "配置全局 QMT 执行参数" not in source
    assert 'out["live"]' not in source
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_ui_settings.py -q`

Expected: FAIL because settings still contains the old inline QMT form.

- [ ] **Step 3: Implement `ui/live_profiles.py`**

Render existing profiles plus a “＋新增配置” choice. Existing IDs are disabled; new IDs are
validated before save. Render QMT fields `account`, `quote_mode`, `price_offset`, and
`max_order_qty`. Save through `upsert_live_profile` + `save_live_profiles`; set default through
`set_default_live_profile`; delete through `delete_live_profile` and show every reference or
parse error returned by `ProfileDeleteBlocked`.

- [ ] **Step 4: Split settings page into tabs and remove old live section**

Keep all current system fields, including the dirty-worktree `qmt_rpc` section. Wrap the existing
form in the “系统设置” tab and call `render_live_profiles_page()` in the “实盘配置” tab.

- [ ] **Step 5: Run UI tests and import smoke test**

Run: `python3 -m pytest tests/test_ui_settings.py tests/test_live_profile_crud.py -q`

Run: `python3 -c "import ui.settings_page, ui.live_profiles"`

Expected: PASS without Streamlit duplicate-key exceptions during import.

- [ ] **Step 6: Commit only live-profile UI hunks**

Stage `ui/live_profiles.py`, `ui/settings_page.py`, and `tests/test_ui_settings.py`. If
`config/settings.yaml` contains unrelated pre-existing `qmt_rpc` changes, do not stage them in
this task.

```bash
git commit -m "feat: manage named live profiles in settings"
```

---

### Task 5: 策略配置改为引用选择

**Files:**
- Modify: `ui/common.py`
- Modify: `ui/strategy_config.py`
- Modify: `config/strategies/combo_turtle_xsmom_x2.yaml`
- Modify: `config/strategies/dca_cn_ndx_gold.yaml`
- Modify: `config/strategies/donchian_etf.yaml`
- Modify: `config/strategies/livermore_etf.yaml`
- Modify: `config/strategies/turtle_s1_etf_hybrid26.yaml`
- Modify: `config/strategies/xsmom_etf_h26.yaml`
- Modify: `tests/test_live_profiles.py`
- Modify: `tests/test_ui_settings.py`

**Interfaces:**
- Consumes: `LiveProfilesCfg`
- Changes: `render_param_form(d, prefix, live_profiles=None) -> dict`
- Produces: `validate_strategy_live_profile(cfg, profiles) -> None`

- [ ] **Step 1: Write failing strategy-reference tests**

```python
def test_all_repo_strategies_explicitly_reference_existing_profile():
    profiles = load_live_profiles()
    for path in sorted((ROOT / "config/strategies").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        assert raw.get("live_profile"), path.name
        assert "qmt" not in raw, path.name
        cfg = load_strategy(path)
        resolve_live_profile(profiles, cfg)

def test_strategy_form_source_has_reference_not_inline_qmt():
    source = Path("ui/common.py").read_text()
    assert "live_profile" in source
    assert "QMT 实盘执行(策略级覆盖" not in source
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_live_profiles.py tests/test_ui_settings.py -q`

Expected: FAIL because strategy YAMLs and the form still use inline QMT.

- [ ] **Step 3: Implement selector and save-time validation**

`render_param_form` loads profiles when not injected, selects `d.get("live_profile")` or the
collection default, writes only `out["live_profile"]`, and renders a read-only QMT summary.
`ui.strategy_config._parse` constructs `StrategyCfg`, loads profiles, calls
`resolve_live_profile`, and returns the validated strategy.

- [ ] **Step 4: Migrate every strategy YAML**

Insert `live_profile: local_qmt` next to `strategy:` in all six files. Assert no repository
strategy contains a top-level `qmt` key before removing the old form.

- [ ] **Step 5: Run focused strategy/UI tests**

Run: `python3 -m pytest tests/test_live_profiles.py tests/test_ui_settings.py tests/test_combo.py tests/test_dca.py tests/test_donchian.py tests/test_livermore.py tests/test_turtle.py tests/test_xsmom.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add ui/common.py ui/strategy_config.py config/strategies tests/test_live_profiles.py tests/test_ui_settings.py
git commit -m "feat: link strategies to reusable live profiles"
```

---

### Task 6: 清理旧配置、文档与端到端验证

**Files:**
- Modify: `config/settings.yaml`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: any tests still importing `LiveCfg`, `check_qmt`, or `resolve_qmt`

**Interfaces:**
- Removes: old `live:` section from `settings.yaml`
- Preserves: existing `qmt_rpc:` section exactly

- [ ] **Step 1: Write/extend repository contract assertions**

```python
def test_settings_has_no_legacy_live_qmt_section():
    raw = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    assert "live" not in raw
    assert "qmt_rpc" in raw

def test_no_production_code_uses_legacy_resolver():
    for path in [Path("scripts/run_signal.py"), Path("ui/common.py"), Path("ui/settings_page.py")]:
        assert "resolve_qmt" not in path.read_text()
```

- [ ] **Step 2: Run assertions and verify RED**

Run: `python3 -m pytest tests/test_live_profiles.py tests/test_ui_settings.py -q`

Expected: FAIL while the legacy `live:` section remains.

- [ ] **Step 3: Remove only the legacy settings hunk and update docs**

Remove `settings.yaml:live`, preserve the pre-existing uncommitted `qmt_rpc` block byte-for-byte,
and update README/AGENTS with the new file, CRUD rules, strategy reference contract, and test
count. Do not stage `docs/qmt-deploy.md`, `qmt_shell/sinan_qmt.py`, `tests/test_qmt_bridge.py`,
`scripts/watch_qmt_live.py`, `sinan/live/qmt_live.py`, or `tests/test_qmt_live.py`.

- [ ] **Step 4: Run the entire test suite**

Run: `python3 -m pytest tests/ -q`

Expected: all tests pass, including the pre-existing uncommitted QMT live-monitor tests.

- [ ] **Step 5: Run behavior-preservation checks**

Run: `python3 -m pytest tests/test_snapshot.py tests/test_engine.py tests/test_call_contract.py -q`

Run: `python3 scripts/run_backtest.py --strategy config/strategies/combo_turtle_xsmom_x2.yaml --start 2015-01-05 --end 2026-08-07`

Expected: snapshot tests pass; combo backtest remains approximately annual return 11.08%, max
drawdown -16.91%, Sharpe 1.05.

- [ ] **Step 6: Test the Streamlit workflow manually**

Start: `streamlit run app.py --server.headless true --server.port 8501`

Verify in browser:

1. “设置 → 实盘配置” shows `本地 QMT (local_qmt)` as default.
2. Add `paper_qmt`, edit its algo, and set it as default.
3. Strategy page can select `paper_qmt` and saves only `live_profile: paper_qmt`.
4. Deleting `paper_qmt` is blocked and lists the strategy.
5. Reassign the strategy, switch default, then delete succeeds.
6. Restore files to the repository defaults after the probe.

- [ ] **Step 7: Review staged scope and commit**

Run: `git diff --cached --check` and inspect `git diff --cached --stat`.

The staged diff must not contain the pre-existing QMT live-monitor work. Commit:

```bash
git commit -m "docs: document reusable live profile workflow"
```

---

## Plan Self-Review

- Spec coverage: model, single source, runtime resolution, CRUD, delete protection, strategy
  selector, migration, errors, tests and UI verification each map to a task.
- Placeholder scan: no deferred implementation or unspecified error handling remains.
- Type consistency: all later tasks consume the Task 1 names and return types unchanged.
- Scope isolation: QMT thin shell and current live-monitor work are explicitly out of scope.
