# QMT 部署指南(Windows ECS)

大 QMT 跑在 Windows ECS 上,司南在本地 Mac。**ECS 侧只需一个脚本**
`qmt_shell/sinan_qmt.py`,三件事全包:

| 职责 | 节奏 | 说明 |
|---|---|---|
| 执行当日全部策略的 targets | 14:45 | 先写执行日志，再以短确定性备注最多一次报单 |
| 按真实成交回写各策略 fills | 15:05 | 备注归因 deals → baseline 幂等重演 → fills |
| 实盘状态推送 | 每 5 秒 | 零/部分/全部成交都刷新执行状态与账户快照 |
| RPC v2 转发 | 常驻 | targets 发布/状态查询在 socket 线程；QMT API 在策略线程泵执行 |

**绑定模拟账号不等于允许模型报单。**仿真验收时，模型必须绑定模拟账号并在
QMT 界面选择“实盘运行”；“模拟运行”只产生模型信号，不会把 `passorder`
送入绑定账号。QMT 未提供稳定字段让脚本读取这个界面开关，因此
`trade_mode=unknown` 只能显示“不可自动检测”，最终以显式交易探针是否取得
委托号为准。资金账号/账号类型仍从绑定关系直读，不需要重复配置。

QMT 内置 Python 是 3.6.8。薄壳不使用 dataclass、现代类型注解或
`datetime.fromisoformat`；升级脚本后应先完成本文的无副作用验证，再执行仿真探针。

## 一、QMT 侧部署(ECS)

1. 大 QMT → 新建模型 → 整文件粘贴 `sinan_qmt.py`；
2. 绑定目标账号；仿真账号也要把模型切到“实盘运行”才能验证真实报单链路；
3. 首次运行会自动生成 `C:\sinan\config\qmt.json`，RPC 默认关闭；
4. 编辑这一个配置文件，填好 Token、白名单并把 `rpc.enable` 改为 `true`；
5. 停止并重新启动策略，配置即生效，无需重启整个 QMT 客户端。

配置示例见 `qmt_shell/qmt.local.example.json`。服务端的共享目录、实盘推送、
RPC、Token 和白名单都只维护在这一份 JSON 中；以后升级只需整份替换脚本。

首次运行前也可以用管理员 PowerShell 一次性创建安全配置：

```powershell
$configDir = 'C:\sinan\config'
$configPath = Join-Path $configDir 'qmt.json'
$tokenBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($tokenBytes)
$rpcToken = [Convert]::ToBase64String($tokenBytes)
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
@{
  share_dir = 'C:\sinan\var\runtime'
  rpc = @{
    enable = $false
    host = '0.0.0.0'
    port = 58620
    token = $rpcToken
    allow_trade = $true
    allow_ips = @('替换为本机出口IP')
  }
  live_push = @{enable = $true; period = '5nSecond'}
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $configPath
icacls $configPath /inheritance:r /grant:r "$env:USERNAME`:(F)" | Out-Null
Write-Host "配置已生成: $configPath"
Write-Host "RPC Token: $rpcToken"
```

把输出的 Token 写入本地 Mac 的 `~/.qmt_rpc_token`，确认白名单后将 JSON 中
`enable` 改为 `true`，再重启策略。不要把 PowerShell 输出或 JSON 提交到仓库。

只能有一个 QMT 模型设置 `rpc.enable = true`。Windows 端使用独占端口，
第二个模型再监听 58620 会明确报端口占用，避免连接被多个模型随机分流。
启动日志会打印实际脚本路径、生效白名单和 Token 长度，不会打印 Token 内容。
脚本实现 QMT 官方 `stop(ContextInfo)` 停止回调：停止模型时主动关闭后台
socket 并释放 58620，因此之后可直接停止/启动模型，无需退出整个 QMT 客户端。
首次从没有 `stop` 回调的旧版升级时，旧监听无法再被新脚本访问，仍需完整退出
QMT 一次清理；此后使用新版脚本即可直接热重启模型。

多策略共账号语义:每个策略一本虚拟账本(现金以 targets 的 capital 开账,
状态在 `SHARE_DIR/state/`),差额各算各的、备注各打各的。每个执行日另有
`SHARE_DIR/executions/execution_{策略}_{YYYYMMDD}.json`，在调用 `passorder`
之前先落 `submitting`。重启遇到无法证明是否已经报出的窗口会标为
`uncertain` 并停止自动重报，避免重复订单；重启恢复会先按备注查询柜台委托/
成交，确认已有记录后才继续剩余计划。RPC 替换 targets 与调仓共用同一临界区，
因此不会出现“文件已换新目标、实际仍下旧目标”的交错。
QMT 要求投资备注少于 24 字符，因此新备注只保存策略/日期哈希与序号，完整身份
由 execution journal 反查；旧版长备注仍兼容读取。

执行事实严格分层：

```text
targets = 目标意图
orders  = 提交过程与柜台委托状态
fills   = QMT 返回的实际 deals（只有它会改变策略账本）
```

`passorder` 返回不再被当作成交。零成交同样写 fills，部分成交只按实际成交额和
手续费更新账本（旧 QMT 字段拼写也兼容）；废单、撤单和未决状态保留在
`orders`，不会污染 `fills`。发布与加载都会校验 `generated_at`，拒绝缺失、
超过 8 小时或超前 5 分钟以上的目标。

## 二、远程访问安全模型(必读)

协议是明文 TCP:**传输安全交给隧道,应用层 token 是第二道锁,不是唯一的锁。**

### 方案 A:SSH 隧道(推荐,零暴露)

**ECS 侧(管理员 PowerShell)**:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

云控制台安全组:只放行 22 端口,授权对象填你本机公网 IP(如 `x.x.x.x/32`,
IP 会变就按段放行)——网络层物理隔离的第一道。`sinan_qmt.py` 保持
`rpc.host = "127.0.0.1"`(RPC 端口对公网零暴露)。

**本机侧**:`~/.ssh/config` 加一段,免记参数:

```
Host qmt-ecs
    HostName <ECS公网IP>
    User Administrator          # Windows 登录用户
    LocalForward 58620 127.0.0.1:58620
    ServerAliveInterval 30
```

开隧道(挂后台保活可用 autossh,`brew install autossh`):

```bash
ssh -N qmt-ecs
# 或:autossh -M 0 -f -N qmt-ecs
```

之后本地一切照常(host 就是 127.0.0.1:58620)——流量全程加密。

### 方案 B:Tailscale(推荐,免隧道运维)

两端安装 Tailscale 登录同一账号；`qmt.json` 的 `rpc.host` 改绑 ECS 的
`100.x.y.z` 虚拟网卡 IP，并配置强 Token（≥32 位随机）与 `allow_ips`；
本地“设置 → 实盘配置”默认项的“QMT 数据连接”填该 IP。
公网不可见,私网可达。

### 方案 C:直接暴露公网端口(不推荐)

仅当:ECS 安全组把 58620 收紧到你的固定出口 IP + `TOKEN` ≥32 位随机 +
`rpc.allow_trade = false`(只读,禁下单)三者同时满足才可接受。
明文协议意味着链路上的观察者可见请求内容——交易通道请回到方案 A/B。

### 服务端强制与开关

- 非 `127.0.0.1` 绑定必须**同时**配置非空 `TOKEN` + 非空 `ALLOW_IPS`
  白名单,缺一拒绝启动——白名单是网络层物理隔离,token 是应用层口令,
  两层独立起效;
- `ALLOW_IPS` 支持单 IP 与 CIDR 网段:Tailscale 场景填本机的 `100.x` IP
  (最严)或 `["100.64.0.0/10"]`(全 Tailscale 网段);直暴场景填家庭/办公
  固定出口 IP。**ECS 安全组同样要收紧到相同来源——两层白名单互为备份**
  (安全组防的是端口扫描,应用层防的是安全组误配);
- token 恒时比较(防时序侧信道);非白名单连接在握手层直接断开并记日志;
- `rpc.allow_trade = false` 把通道降为只读:行情/账户查询照常,
  `passorder`/`cancel` 一律拒绝——远端调试建议默认只读,要下单时再开。
- 自动生成配置的 `rpc.allow_trade` 默认是 `true`，但 RPC 本身默认关闭；启用并
  部署到公网前必须确认 Token、
  应用白名单和云安全组均已收紧。需要只读调试时显式改为 `False`。
- RPC v2 限制单请求为 1 MiB、队列为 64 项，并只开放司南使用的 QMT API；
  后台 socket 线程不直接调用 QMT C++ API，而是交给 1 秒策略线程请求泵；
- `rpc.health` 直接返回协议和能力，不进入 QMT API 队列；当前能力包括
  `qmt_api_queue`、`publish_targets`、`execution_status`；
- “设置 → 实盘配置 → 验证 RPC”会依次验证协议、`510300.SH` 行情、绑定账号、
  委托和成交查询可序列化，**不会**调用发布、下单或撤单函数。

## 三、本地侧配置(司南)

1. 在`设置 → 实盘配置`选择默认配置，在“QMT 数据连接”配置 host/port
   (SSH 隧道场景就是 127.0.0.1:58620);
2. token 写入本机 `~/.qmt_rpc_token`(单行):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.qmt_rpc_token && chmod 600 ~/.qmt_rpc_token
```

   ECS 侧 `C:\sinan\config\qmt.json` 的 `rpc.token` 填同一串。Token 只允许保存在
   服务器本地 JSON 和 Mac 的私有文件中，严禁写进脚本、仓库、共享目录或日志。
3. 先在设置页点击“验证 RPC”。成功只说明网络、协议、行情、账号和交易查询可用，
   `RPC 交易转发：允许`也只说明服务端没有拦截交易，不代表 QMT 模型界面已切到
   “实盘运行”。
4. 查询 API 可直接使用同名 SDK:

```python
from qmt_shell import qmt_sdk as qmt
qmt.connect_from_settings()          # 读默认实盘配置 qmt.rpc + ~/.qmt_rpc_token
accs = qmt.get_trade_detail_data("8888888888", "STOCK", "account")
```

## 四、显式发布与拉取

`run_signal.py` 只生成并保存本地 targets，永远不连接 QMT、不自动发布、不下单。
远端执行必须是独立的显式动作：

```bash
# 先正常生成目标意图
python3 scripts/run_signal.py --strategy config/strategies/combo_turtle_xsmom_x2.yaml \
  --date 2026-08-21

# 再把这一个文件发布到 payload.live_profile 指向的精确实盘配置
python3 scripts/publish_targets.py \
  var/runtime/targets/targets_combo_turtle_xsmom_x2_20260821.json

# 成交/终态出现后，可重复发布（返回 duplicate）并原子拉回 fills
python3 scripts/publish_targets.py --pull \
  var/runtime/targets/targets_combo_turtle_xsmom_x2_20260821.json
```

服务端自己构造文件名并校验策略名、日期、checksum 和大小。完整 payload 相同才
返回 `duplicate`；即使权重 checksum 相同，只要生成时间或算法参数变化也按替换
处理。执行开始前可替换目标，进入 `submitting` 后任何变更都会被拒绝。
因此远程模式不依赖 Mac 与 Windows 之间另设目录同步，RPC 发布是 targets 的权威
传输路径；本地原文件仍是可审计意图。

仿真账号首次上线，用独立探针验证一次真实报单路径。它会提交一笔指定价委托，
按短唯一备注查询，取得可撤委托号后请求撤单并持续查询到撤单/成交/拒单终态；
未确认终态、超时或异常均返回 `uncertain`，绝不重新报单：

```bash
python3 scripts/qmt_trade_probe.py \
  --confirm-simulation-account 80391000 \
  --symbol 510300.SH --qty 100 --limit-price <正数限价>
```

必须逐字确认当前仿真账号。限价应由操作者选择为不易成交但符合券商价格规则的
有效价格；若已经成交，探针只报告 deal，不会反向下单。普通“验证 RPC”按钮与此
探针严格分离。

## 五、升级、回滚与故障判断

升级：停止 QMT 模型，整份替换 `sinan_qmt.py`，重新启动模型；
`C:\sinan\config\qmt.json` 不随脚本替换。看到 protocol v2 和三个能力后再继续。

回滚：先停止模型并确认日志出现“监听端口已释放”，保存
`SHARE_DIR/executions/` 与 `fills/`，再粘贴上一版脚本并启动。若任何日志停在
`submitting`/`uncertain`，必须先在 QMT 委托/成交中按 remark 人工核对，不能删除
日志后重报。旧版不认识 v2 journal 时只允许做只读回滚诊断，不应继续自动下单。

公网明文 RPC 即使同时设置 Token、应用白名单和云安全组，也只适合临时只读诊断；
任何 `allow_trade=true` 的远程交易必须迁移到 SSH 隧道或 Tailscale。

## 六、安全清单(上实盘前逐项过)

- [ ] RPC 转发未直接暴露公网(SSH 隧道或 Tailscale)
- [ ] TOKEN ≥32 位随机,两端一致,`~/.qmt_rpc_token` 权限 600
- [ ] 远端调试期 `rpc.allow_trade = false`,确认要下单才打开
- [ ] ECS 安全组最小开放(方案 A 仅 22;方案 B 零公网端口)
- [ ] fills/targets 同步盘目录不含任何凭证
- [ ] 设置页协议 v2、行情、账号、委托/成交查询全部通过
- [ ] 绑定仿真账号的模型已切到“实盘运行”，交易探针取得唯一委托号并确认终态
- [ ] 任一 `uncertain` 执行已经人工按 remark 核对，未通过删除日志盲目重报
- [ ] 影子模式已并行跑 2~4 周；`trade_mode=unknown` 未被误解为已就绪
