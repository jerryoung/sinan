# 可复用实盘配置设计

## 目标

把 QMT 下单参数从系统全局配置和策略 YAML 的重复内联字段中抽离为独立的
“实盘配置”。系统设置负责实盘配置的新增、编辑、设为默认和删除；策略只保存
一个稳定的配置 ID 引用。修改一份实盘配置后，所有引用它的策略统一生效。

本次尚未投入实盘，采用干净切换：删除旧的 `Settings.live.qmt` 与
`StrategyCfg.qmt` 入口，不保留两套语义并存的兼容层。

## 配置文件与数据模型

新增 `config/live_profiles.yaml`，它是实盘配置的唯一事实来源：

```yaml
default: local_qmt

profiles:
  local_qmt:
    name: 本地 QMT
    engine: qmt
    qmt:
      algo:
        quote_mode: latest
        price_offset: 0.002
        max_order_qty: 10000
```

规则如下：

- 配置 ID 是 `profiles` 的键，只允许小写字母开头，以及小写字母、数字、
  `_`、`-`；创建后不可修改。
- `name` 是可编辑的展示名称。
- `engine` 当前只接受 `qmt`，字段保留用于未来扩展其他实盘引擎。
- `qmt.account` 可选，延续当前“仅随 targets 留痕、多账号扩展预留”的语义；
  QMT 薄壳仍按模型绑定账号下单，界面必须明确提示。
- `qmt.algo.quote_mode` 只能是 `latest` 或 `limit`。
- `price_offset` 必须是大于或等于 0 的浮点数，`max_order_qty` 必须是正整数。
- `default` 必须指向已存在的配置，`profiles` 不允许为空。
- 文件保存采用临时文件替换，避免中途写入造成半份 YAML。

对应的配置模型放在 `sinan/config.py`：

- `QmtAlgoCfg`
- `QmtExecutionCfg`
- `LiveProfileCfg`
- `LiveProfilesCfg`
- `load_live_profiles(path=None)`
- `save_live_profiles(config, path=None)`
- `resolve_live_profile(profiles, strategy_cfg)`

`StrategyCfg` 删除 `qmt`，新增：

```python
live_profile: str = "local_qmt"
```

仓库内所有策略 YAML 都显式写入：

```yaml
live_profile: local_qmt
```

模型保留默认值是为了测试和程序化创建策略时有安全缺省；仓库配置文件仍显式
保存引用，使策略与实盘配置的关系可见、可审计。

## 解析与执行数据流

出信号时的数据流改为：

```text
StrategyCfg.live_profile
    -> LiveProfilesCfg.profiles[profile_id]
    -> resolve_live_profile()
    -> targets.live_profile + targets.qmt
    -> 现有 QMT 薄壳
```

`resolve_live_profile` 必须执行以下校验：

1. 策略引用不能为空。
2. 引用的配置必须存在；不存在时直接拒绝生成 targets，不静默回退。
3. 配置必须通过对应引擎的结构校验。
4. 返回深拷贝，调用方不得污染长驻进程中的配置对象。

`run_signal.py` 加载 `live_profiles.yaml` 并解析策略引用。targets 继续输出当前
QMT 薄壳消费的 `qmt` 字段，因此薄壳协议和执行代码无需修改；同时新增
`live_profile` 字段用于运行留痕。回测不读取实盘配置，不受该变化影响。

`qmt_rpc` 收敛为每份 QMT 实盘配置中的 `qmt.rpc`。策略引用的配置决定其
执行参数；系统级 QMT 数据源固定读取默认实盘配置的连接参数。RPC 字段不写入
targets，token 仍只存 `~/.qmt_rpc_token`。

## 设置界面

“设置”页拆成两个页签：

1. **系统设置**：保留本金、执行、风控、数据源、通知和高级 YAML。
2. **实盘配置**：管理 `config/live_profiles.yaml`。

实盘配置页行为：

- 顶部显示默认配置和配置数量。
- 配置选择器包含已有配置与“新增配置”。
- 新增时填写不可重复的 ID、展示名称、QMT 执行参数和 QMT 数据连接。
- 编辑时 ID 只读，展示名称及 QMT 参数可修改。
- 可把任一配置设为默认；默认配置在列表中标记。
- 删除前扫描 `config/strategies/*.yaml`：
  - 当前默认配置禁止删除，提示先切换默认配置；
  - 被策略引用的配置禁止删除，并列出策略展示名、策略 ID 和文件名；
  - 仅未被引用且非默认的配置可删除。
- 所有保存先经 `LiveProfilesCfg` 校验；错误留在页面，不写文件。
- 控件 key 继续绑定文件内容指纹，避免文件外部变化后旧控件状态回写。

为保持职责单一，集合的加载、校验、引用扫描和增删改逻辑放在
`sinan/live/profiles.py`；Streamlit 展示放在 `ui/live_profiles.py`，
`ui/settings_page.py` 只负责页签编排和原系统设置表单。

## 策略配置界面

删除当前“QMT 实盘执行（策略级覆盖）”内联表单，替换为单个“实盘配置”
下拉框：

- 选项来自 `live_profiles.yaml`；显示“展示名称（配置 ID）”。
- 新策略默认选中 `default` 指向的配置。
- 保存前确认引用仍然存在；若另一页面删除或修改配置造成引用失效，拒绝保存并
  给出明确错误。
- 下拉框旁显示只读摘要：引擎、报价方式、限价偏移和拆单上限，便于确认引用，
  但策略页不允许修改这些参数。
- 提供“前往设置 → 实盘配置修改”的提示，确保只有一个编辑入口。

## 删除与一致性约束

删除采用 fail-closed 语义。配置仍是默认配置或仍有任何策略引用时，删除操作
不会修改文件。引用列表由磁盘上的策略 YAML 现读，不依赖 Streamlit 缓存。

如果策略文件无法解析，为避免漏判引用，删除操作同样拒绝，并列出无法解析的
文件；修复策略配置后才能继续删除。

配置 ID 不支持原地重命名。需要改 ID 时，先新增配置，再逐个调整策略引用和
默认配置，最后删除旧配置。这样每一步都可校验，不会产生悬空引用。

## 迁移

本次一次性迁移包括：

1. 新增默认 `local_qmt` 配置，参数采用当前界面默认值。
2. 从 `settings.yaml` 删除旧 `live` 段。
3. 从 `StrategyCfg` 和策略配置表单删除内联 `qmt`。
4. 为仓库内每份策略 YAML 显式加入 `live_profile: local_qmt`。
5. 将 `resolve_qmt` 替换为 `resolve_live_profile`，更新 run_signal、文档和测试。

由于系统尚未投入使用，不读取或迁移任意未知的旧内联 QMT 配置；如果迁移时
发现仓库策略中已有非空 `qmt`，测试应失败并要求人工把它转换成命名配置，避免
静默丢失参数。

## 错误处理

- YAML 语法错误、重复 ID、非法 ID、空配置集合、默认配置不存在：拒绝保存。
- 策略引用不存在：策略保存和出信号都拒绝，错误包含策略 ID 与配置 ID。
- 引擎类型未知：配置加载时拒绝。
- 删除默认或被引用配置：不写文件，页面列出阻止原因。
- 文件写入失败：保留原文件，页面显示异常。
- 旧 targets 不含 `live_profile` 时，展示和 QMT 薄壳继续兼容。

## 测试与验收

测试按 TDD 编写，至少覆盖：

1. 默认 `local_qmt` 文件可解析，默认引用存在。
2. 非法/重复 ID、空集合、悬空默认配置、非法 QMT 参数被拒绝。
3. 策略引用配置后解析出正确 QMT 参数且返回深拷贝。
4. 策略引用不存在时拒绝，不回退到默认配置。
5. 默认配置及被策略引用的配置不能删除，错误列出引用策略。
6. 未引用且非默认配置可以删除。
7. 保存失败不会破坏原配置文件。
8. 策略表单只保存 `live_profile`，不再生成 `qmt`。
9. run_signal 生成的 payload 同时包含 `live_profile` 和兼容的 `qmt`。
10. 仓库所有策略 YAML 显式引用存在的配置，且不存在旧 `qmt` 字段。
11. 全量测试及回测快照通过，证明研究流程未受实盘配置重构影响。

验收标准是：QMT 参数只有 `config/live_profiles.yaml` 一个可编辑事实来源；
策略只通过 ID 引用；任何删除、保存或执行都不能产生悬空引用或静默回退。
