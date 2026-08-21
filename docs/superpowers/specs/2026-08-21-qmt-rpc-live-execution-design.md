# QMT RPC 实盘执行桥接设计

## 1. 目标

在不改变 `generate_targets` 回测/实盘共用契约的前提下，把本机司南与远端
大 QMT 连接成可验证、可恢复、可审计的完整执行链路：

```text
generate_targets
  -> 本机 targets 留痕
  -> RPC 幂等发布
  -> QMT 本机持久化
  -> 委托与状态跟踪
  -> 实际成交
  -> fills / 策略账本
  -> 本机拉取与次日对账
```

本设计同时修复本轮仿真验证暴露的三个问题：

1. QMT 委托/成交对象的 `m_xtTag` 不能转换，导致整个 RPC 查询失败；
2. 客户端关闭后，服务端把 Windows `10053/10054` 当成线程异常打印；
3. 当前薄壳调用 `passorder` 后立即修改账本并写 `fills`，把“提交委托”误当成
   “实际成交”；
4. QMT 日志显示内置环境为 Python 3.6，而当前 targets 校验调用
   `datetime.fromisoformat`（Python 3.7 才提供），一旦真正读取当日 targets 就会
   在执行前失败。

## 2. 非目标

- 不修改任何策略、信号参数、风险算法或回测会计口径；
- 不改变 `targets` 的权重 checksum 算法；
- 不自动启用真实资金交易；
- 不实现多资金账号分账；`qmt.account` 仍只留痕，实际账号由 QMT 模型绑定；
- 不依赖网盘、SMB 或其他仓库外目录同步程序；若用户已有同步，幂等校验仍能
  容忍同一份文件被重复送达；
- 不在本轮加入通用远程文件读写能力，只开放有限的 targets 发布和执行状态读取。

## 3. 第一性原理与不变量

### 3.1 真相来源

- `targets` 只代表策略意图；
- `passorder` 只代表调用已发出，QMT 官方接口没有同步返回成交结果；
- 委托查询代表柜台是否接收及当前委托状态；
- 成交查询中的 deal 才是修改策略现金、持仓和 `fills` 的唯一依据；
- 同一批 deals 从同一份调仓前账本重演，结果必须幂等。

### 3.2 失败取舍

无法证明“订单没有发出”时，优先停止并标记 `uncertain`，不得自动重报。少下一单
可以被告警和人工修复；重复下单可能扩大真实敞口，风险不可对称。

### 3.3 跨机边界

RPC 负责传输，QMT 本机 `SHARE_DIR` 负责持久化。网络中断不能丢失已经接收的
targets，也不能让 QMT 重启后忘记已提交的委托。

## 4. 模块与职责

### 4.1 本机 `QmtRpcBridge`

新增中立的本机执行桥接模块，职责仅包括：

- 从命名实盘配置读取 host、port、timeout，token 仍读取
  `~/.qmt_rpc_token`；
- 发布一份已经由 `sinan.live.targets` 构造并校验的 payload；
- 查询指定策略和执行日的远端执行状态；
- 把远端实际 `fills` 原子保存到本机 `settings.fills_dir`；
- 返回结构化结果，不包含 Streamlit 展示逻辑。

生产入口采用显式动作：`run_signal.py` 继续只生成本机 targets，不因一次普通的
“更新数据并生成 targets”自动触发交易。新增独立的
`scripts/publish_targets.py` 命令；UI 对应操作必须与“只生成 targets”分开，并
明确展示绑定账号和配置 ID。这样影子模式不会因配置了 RPC 而静默升级成交易
模式。

### 4.2 QMT RPC 保留方法

RPC 协议升级为 v2，并增加 capabilities。开放以下窄接口：

- `rpc.health`：服务、协议、账号、交易转发开关、能力列表和服务器时间；
- `rpc.publish_targets(payload)`：校验并原子写入 QMT 的 targets 目录；
- `rpc.execution_status(strategy, date)`：读取执行日志与 fills；
- 现有行情、账户、委托、成交和交易 API 转发继续兼容。

`rpc.publish_targets` 属于交易能力，受 `rpc.allow_trade` 控制。服务端必须重新计算
checksum，验证策略名、日期、生成时间和路径安全；不得信任客户端给出的文件名。

发布幂等键为 `(strategy, date, checksum)`：

- 相同日期、策略和 checksum：返回 `duplicate`，不重复写入或执行；
- 同一日期和策略、不同 checksum、尚未开始执行：原子替换并记录版本；
- 已进入 `submitting` 或更后状态：拒绝覆盖，要求人工处理。

### 4.3 QMT API 主线程泵

当前 socket 工作线程直接调用 QMT C++ API，实测行情/账户/委托查询约 5–10 秒，
且 QMT 官方运行模型以策略回调线程为中心。新设计把调用分为两类：

- `rpc.health`、targets 发布、执行状态读取等纯 Python/文件操作可在 socket 线程
  完成；
- `C.*`、`get_trade_detail_data`、`passorder`、`cancel` 等 QMT API 请求进入有界
  队列，由 `C.run_time` 注册的 1 秒 RPC pump 在 QMT 策略线程执行。

请求带唯一 ID、截止时间和结果事件。pump 每轮按数量和耗时上限处理，避免 RPC
流量阻塞调仓和实盘推送。先通过仿真环境测量延迟：若线程归属假设成立，P95 应从
约 10 秒降到 2 秒以内；若不成立，保留队列的线程安全收益，但以实测结果调整
timeout，不把推测写成已实现承诺。

只读请求在“明确尚未送达服务端”时允许重连一次；`passorder`、`cancel` 和
`rpc.publish_targets` 不做客户端自动重试。发布 targets 的重复调用由服务端
幂等键消除，而交易 API 没有同等级的同步幂等保证。

### 4.4 安全序列化

QMT PythonObj 继续以 `m_*` 字段映射为 JSON，但逐字段安全读取：

- `getattr` 或递归转换失败时只跳过该不可转换字段；
- 不允许单个 `m_xtTag/CXtOrderTag` 破坏整个 order/deal 列表；
- 订单与成交所需关键字段必须在测试中锁定，包括 remark、系统委托号、状态、
  原始数量、成交数量、成交价格、成交号和时间；
- 关键字段缺失时执行状态标为 `unreadable`，不得假装为空列表。

socket 服务端把对端正常关闭、`ConnectionAbortedError`、
`ConnectionResetError`、`BrokenPipeError` 以及对应 Windows 错误码作为连接级事件
结束处理，不打印线程 traceback。客户端关闭顺序为 reader、shutdown、socket，
避免用重置连接代替正常 EOF。

QMT 薄壳继续兼容其内置 Python 3.6：不得使用 dataclass、现代类型注解或
`datetime.fromisoformat` 等 3.7+ API；ISO 时间由一个经过测试的兼容解析函数统一
处理。

## 5. 执行日志与状态机

QMT 在 `SHARE_DIR/executions/` 为每个策略执行日维护一份原子写入的执行日志：

```text
received
  -> planned
  -> submitting
  -> submitted
  -> accepted -> partially_filled -> filled
       |               |
       +-----> canceled <+
  submitted -> rejected
  submitting / submitted -> uncertain
```

执行日志至少包含：

- strategy、date、target checksum、创建和更新时间；
- 调仓前 baseline ledger；
- 每笔计划的 sequence、确定性 remark、symbol、side、qty、报价参数；
- submitted_at、order_sys_id、order_status、traded_qty、cancel_qty、error；
- 去重后的实际 deals；
- 当前总状态及需要人工处理的原因。

### 5.1 防重复报单

每笔委托使用少于 24 字符的确定性短备注（策略 ID + 日期哈希、序号），完整
strategy/date/sequence 保存在 execution journal 并建立反向索引；旧版
`策略ID#YYYYMMDD#序号` 继续兼容读取。调用 `passorder` 前先把该笔状态持久化为
`submitting`：

- 重启看到 `accepted` 或更后状态，不再提交；
- 重启看到 `submitting/submitted`，先按 remark 查询委托和成交；
- 在 QMT 查询缓存可能延迟的窗口内持续等待；
- 仍无法证明是否已提交时标记 `uncertain` 并告警，不自动重报。

这是“最多一次”优先策略，避免进程恰好在 `passorder` 与日志更新之间退出后产生
重复订单。

### 5.2 委托与成交归因

委托和成交都按 remark 中的策略 ID、执行日和序号归因。系统委托号和成交号用于
二次去重；同一 deal 被多次轮询或在重启后重新读取，不得重复记账。

## 6. fills 与策略账本

`do_rebalance` 不再预先修改账本，也不再把 planned/submitted orders 写入
`fills`。执行后每次对账都执行：

1. 读取持久化 baseline ledger；
2. 收集并去重该策略、该执行日的实际 deals；
3. 从 baseline 按真实成交额和手续费幂等重演（金额缺失时才回退数量×价格）；
4. 原子保存策略 ledger；
5. 写 `fills_{策略}_{YYYYMMDD}.json`。

fills 新增 `execution_status` 和 `orders`，原有 `fills` 字段只存实际成交：

- 零成交也写文件，账本保持不变，让次日 `reconcile_fills` 发现目标偏差；
- 部分成交只更新实际成交部分；
- 拒单、撤单和未决委托保留在 `orders`，不污染交易记录；
- `weights`、`positions`、`cash` 和 `total_asset` 来自重演后的策略虚拟账本；
- 账户级余额、总资产和持仓继续放在 `qmt_live.json`，不得混成单策略资产。

为兼容现有看板和 `run_signal`，旧 fills 文件仍可读取；新消费者只把
`fills[*]` 当成交，把 `orders[*]` 当执行过程。

## 7. 就绪验证与 UI 语义

“验证 RPC”保持无交易副作用，并分层展示：

1. 网络与 token；
2. RPC 协议与能力；
3. 行情可用；
4. 账号识别与登录状态；
5. 委托/成交查询可序列化；
6. `allow_trade` 仅显示为“RPC 允许交易转发”。

QMT 当前无法可靠暴露模型界面的“模拟运行/实盘运行”开关，因此
`trade_mode=unknown` 必须显示为“QMT 未提供可检测字段”，不能解释为实盘准备
就绪。只有一次显式交易探测进入柜台后，才能记录“报单链路已验证”。

交易探测是独立按钮/命令，要求用户明确确认仿真账号、标的、数量和限价；验证顺序
是提交一次、按唯一 remark 查询、取得系统委托号、撤单或确认成交。普通 readiness
验证绝不调用 `passorder`。

## 8. 安全边界

- token 不进入仓库、日志、targets 或同步目录；
- 非本机绑定继续强制 token 和 IP/CIDR 白名单；
- 公网明文 RPC 即使有白名单仍不适合真实资金，真实交易前必须切换 SSH 隧道或
  Tailscale；当前公网地址只允许用于用户明确授权的仿真验证；
- 服务端限制单请求大小、队列长度和允许调用的方法；
- targets 发布、下单和撤单都记录不含 token 的审计信息；
- 任一策略 payload 或执行失败必须逐策略隔离，不得中断同日其他策略。

## 9. 测试与验收

### 9.1 自动化测试

- 不可转换的 `m_xtTag` 被跳过，order/deal 其他关键字段完整返回；
- 关键字段不可读时返回明确错误而非空成功；
- 正常 EOF、Windows 10053/10054 和 BrokenPipe 不产生未捕获线程异常；
- 只读请求与交易请求遵守不同重试策略；
- targets checksum、策略名、日期、请求大小和路径穿越校验；
- 重复发布返回 duplicate；执行开始后不同 checksum 被拒绝；
- passorder 后、deal 前账本和 fills 不变化；
- 零成交、拒单、撤单、部分成交和全部成交分别得到正确状态；
- 重复 deal、脚本重启和重复调度不重复记账或下单；
- 一个策略失败不影响其他策略；
- 旧 targets/fills 读取兼容；
- Python 3.6 兼容的 targets 时间解析通过测试，QMT 脚本不得依赖 3.7+ API；
- `run_signal` 始终不发送远端交易请求，只有独立发布命令可以发布；
- 全量测试通过，回测快照不变化。

### 9.2 仿真环境验收

部署新版 QMT 脚本并重启绑定仿真账号的“实盘运行”模型后：

1. health、行情、账户、持仓、委托、成交全部可查询；
2. 对 `510300.SH` 或当时价格和可用资金允许的低风险 ETF 提交一笔 100 股测试单；
3. 唯一 remark 出现在委托列表，并取得系统委托号；
4. 未成交则撤单并确认终态；成交则确认 deal、持仓和资金变化；
5. 发布一份专用测试 targets，确认远端落盘且重复发布不重复执行；
6. 验证 execution journal、orders、fills 和策略账本一致；
7. 拉取 fills 到本机，运行 `reconcile_fills` 并核对预期偏差；
8. 重启 QMT 模型，确认 RPC 端口释放/重建且执行日志不会触发重复报单；
9. 记录查询延迟分布，确认是否达到 P95 2 秒目标或据实记录剩余限制。

若验收时间不在交易时段，可以完成查询、发布幂等和拒单状态验证，但不能把它们
替代“柜台接受、撤单或实际成交”的交易时段证据。完整目标保持未完成，直到交易
时段复验成功。

## 10. 迁移与回滚

1. 本机先升级代码和测试，不自动发布 targets；
2. 整文件替换 QMT 的 `sinan_qmt.py`，保留
   `C:\sinan\config\qmt.json`；
3. 停止并启动 QMT 模型，确认 RPC protocol v2 和 capabilities；
4. 完成只读验证后再做仿真交易探测；
5. 发现异常时关闭 `rpc.allow_trade` 即可降级为只读，原 targets 文件桥接和
   `generate_targets` 不受影响；
6. 回滚脚本前保留 `executions/` 和 fills 审计文件，禁止删除实际交易痕迹。
