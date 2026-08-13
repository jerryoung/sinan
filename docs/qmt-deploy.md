# QMT 部署指南(Windows ECS)

大 QMT 跑在 Windows ECS 上,司南在本地 Mac。**ECS 侧只需一个脚本**
`qmt_shell/sinan_qmt.py`,三件事全包:

| 职责 | 节奏 | 说明 |
|---|---|---|
| 执行当日全部策略的 targets | 14:45 | 多策略共账号,下单备注「策略ID#日期#序号」归因 |
| 按成交回报回写各策略 fills | 15:05 | 备注归因 deals → 策略虚拟账本 → fills_{策略}_{日期}.json |
| 实盘状态推送 | 每 5 秒 | 新成交即时修正 fills，账户快照写 state/qmt_live.json |
| API socket 转发(qmt_sdk 对端) | 常驻 | RPC_ENABLE=False 可关 |

模拟盘/实盘:在 QMT 把模型绑到模拟或实盘账号时决定,脚本只如实上报
(fills 的 `trade_mode`);**资金账号/账号类型同样从绑定关系直读,
不需要在脚本里填**。

## 一、QMT 侧部署(ECS)

1. 大 QMT → 新建模型 → 整文件粘贴 `sinan_qmt.py`;
2. 改 `SHARE_DIR` 为与本地同步的目录(坚果云/Syncthing 盘);
3. 如启用远程 RPC，在顶部配置区自行填写 `RPC_TOKEN` 与
   `RPC_ALLOW_IPS`；仓库脚本不预填任何 Token 或白名单；
4. 绑定 模拟/实盘 账号并运行——账号信息自动读取,多策略无须多个模型。

只能有一个 QMT 模型设置 `RPC_ENABLE = True`。Windows 端使用独占端口，
第二个模型再监听 58620 会明确报端口占用，避免连接被多个模型随机分流。
启动日志会打印实际脚本路径、生效白名单和 Token 长度，不会打印 Token 内容。

多策略共账号语义:每个策略一本虚拟账本(现金以 targets 的 capital 开账,
状态在 SHARE_DIR/state/),差额各算各的、备注各打各的,同一标的甚至反向
调仓也不串账;脚本会对"各策略目标权重合计 >100%"给出警告。

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
`RPC_HOST = "127.0.0.1"` 不变(RPC 端口对公网零暴露)。

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

两端安装 Tailscale 登录同一账号;`sinan_qmt.py` 的 `RPC_HOST` 改绑 ECS 的
`100.x.y.z` 虚拟网卡 IP,并配置强 `RPC_TOKEN`(≥32 位随机)与
`RPC_ALLOW_IPS`;本地“设置 → 实盘配置”默认项的“QMT 数据连接”填该 IP。
公网不可见,私网可达。

### 方案 C:直接暴露公网端口(不推荐)

仅当:ECS 安全组把 58620 收紧到你的固定出口 IP + `TOKEN` ≥32 位随机 +
`RPC_ALLOW_TRADE = False`(只读,禁下单)三者同时满足才可接受。
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
- `RPC_ALLOW_TRADE = False` 把通道降为只读:行情/账户查询照常,
  `passorder`/`cancel` 一律拒绝——远端调试建议默认只读,要下单时再开。

## 三、本地侧配置(司南)

1. 在`设置 → 实盘配置`选择默认配置，在“QMT 数据连接”配置 host/port
   (SSH 隧道场景就是 127.0.0.1:58620);
2. token 写入本机 `~/.qmt_rpc_token`(单行):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.qmt_rpc_token && chmod 600 ~/.qmt_rpc_token
```

   ECS 侧 `sinan_qmt.py` 的 `RPC_TOKEN` 填同一串。**token 严禁写进仓库/配置/日志**
   (与 `~/.tushare_token` 同一纪律)。
3. 使用:

```python
from qmt_shell import qmt_sdk as qmt
qmt.connect_from_settings()          # 读默认实盘配置 qmt.rpc + ~/.qmt_rpc_token
accs = qmt.get_trade_detail_data("8888888888", "STOCK", "account")
```

## 四、安全清单(上实盘前逐项过)

- [ ] RPC 转发未直接暴露公网(SSH 隧道或 Tailscale)
- [ ] TOKEN ≥32 位随机,两端一致,`~/.qmt_rpc_token` 权限 600
- [ ] 远端调试期 `RPC_ALLOW_TRADE = False`,确认要下单才打开
- [ ] ECS 安全组最小开放(方案 A 仅 22;方案 B 零公网端口)
- [ ] fills/targets 同步盘目录不含任何凭证
- [ ] 影子模式已并行跑 2~4 周,`trade_mode` 显示与预期一致
